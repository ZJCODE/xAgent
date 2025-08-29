import chromadb
import chromadb.utils.embedding_functions as embedding_functions
from chromadb.config import Settings
import logging
import os
import uuid
from typing import List, Optional, Dict, Any
from pathlib import Path
import dotenv

from .base_vector_store import VectorStoreBase, VectorDoc

dotenv.load_dotenv(override=True)


class VectorStoreLocal(VectorStoreBase):
    """
    Local vector storage using ChromaDB.
    
    This class provides ChromaDB-based vector storage operations including
    upserting, querying, and deleting vector documents.
    
    Args:
        path: Path to ChromaDB storage directory. Defaults to ~/.xagent/chroma
        collection_name: Name of the ChromaDB collection. Defaults to 'xagent_vectors'
        embedding_model: Name of the OpenAI embedding model. Defaults to 'text-embedding-3-small'
    """
    
    def __init__(self, 
                 path: Optional[str] = None,
                 collection_name: str = "xagent_vectors",
                 embedding_model: str = "text-embedding-3-small"):
        # Initialize logger
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        
        # Use default path if none provided
        if path is None:
            path = os.path.expanduser('~/.xagent/chroma')
            self.logger.info("No path provided, using default path: %s", path)
        
        # Ensure the directory exists
        Path(path).mkdir(parents=True, exist_ok=True)
        
        # Initialize OpenAI embedding function
        self.openai_ef = embedding_functions.OpenAIEmbeddingFunction(
            model_name=embedding_model
        )
        
        # Initialize ChromaDB client and collection
        self.chroma_client = chromadb.PersistentClient(
            path=path,
            settings=Settings(anonymized_telemetry=False)
        )
        self.collection = self.chroma_client.get_or_create_collection(
            name=collection_name,
            embedding_function=self.openai_ef
        )
        
        self.logger.info("VectorStoreLocal initialized with collection: %s at path: %s", 
                        collection_name, path)
    
    async def upsert(self,
                     ids: List[str],
                     documents: List[str],
                     metadatas: List[Dict[str, Any]]
                     ):
        """
        Upsert multiple vector documents.
        
        Args:
            ids: List of document IDs
            documents: List of document texts
            metadatas: List of metadata dictionaries
        """
        if not ids or not documents or not metadatas:
            self.logger.warning("Empty input provided to upsert")
            return
        
        if len(ids) != len(documents) or len(ids) != len(metadatas):
            raise ValueError("ids, documents, and metadatas must have the same length")
        
        try:
            self.collection.upsert(
                documents=documents,
                metadatas=metadatas,
                ids=ids
            )
            self.logger.debug("Upserted %d documents to ChromaDB", len(ids))
            
        except Exception as e:
            self.logger.error("Failed to upsert documents: %s", str(e))
            raise
    
    async def query(self,
                    query_texts: Optional[List[str]] = None,
                    n_results: Optional[int] = 5,
                    meta_filter: Optional[Dict[str, Any]] = None,
                    keywords_filter: Optional[List[str]] = None
                    ) -> List[VectorDoc]:
        """
        Query vector documents.
        
        Args:
            query_texts: List of query texts for semantic search
            n_results: Maximum number of results to return
            meta_filter: Metadata filter (supports MongoDB-style queries)
            keywords_filter: List of keywords for document content filtering (OR logic)
            
        Returns:
            List of VectorDoc objects
        """
        if not query_texts:
            raise ValueError("query_texts must be provided for ChromaDB queries")
        
        try:
            # Prepare query parameters
            query_params = {
                "query_texts": query_texts,
                "n_results": n_results or 5,
                "include": ["documents", "metadatas", "distances"]
            }
            
            # Convert meta_filter to ChromaDB where clause
            if meta_filter:
                chroma_where = self._convert_meta_filter_to_chroma(meta_filter)
                query_params["where"] = chroma_where
            
            # Build keyword filter for document content
            if keywords_filter:
                keyword_query = self._build_keyword_query(keywords_filter)
                query_params["where_document"] = keyword_query
            
            # Execute query
            results = self.collection.query(**query_params)
            
            # Convert results to VectorDoc format
            vector_docs = []
            if results.get("documents"):
                for i, query_result_list in enumerate(results["documents"]):
                    for j, document in enumerate(query_result_list):
                        # Get corresponding metadata and distance
                        metadata = results["metadatas"][i][j] if results.get("metadatas") else {}
                        distance = results["distances"][i][j] if results.get("distances") else None
                        doc_id = results["ids"][i][j] if results.get("ids") else str(uuid.uuid4())
                        
                        # Convert distance to similarity score (1 - distance)
                        score = (1.0 - distance) if distance is not None else None
                        
                        vector_doc = VectorDoc(
                            id=doc_id,
                            document=document,
                            metadata=metadata,
                            score=score
                        )
                        vector_docs.append(vector_doc)
            
            self.logger.debug("Query returned %d results from %d query texts", 
                            len(vector_docs), len(query_texts))
            return vector_docs
            
        except Exception as e:
            self.logger.error("Failed to query documents: %s", str(e))
            raise
    
    async def delete(self,
                     ids: List[str]
                     ):
        """
        Delete multiple vector documents by IDs.
        
        Args:
            ids: List of document IDs to delete
        """
        if not ids:
            self.logger.warning("Empty IDs list provided to delete")
            return
        
        try:
            self.collection.delete(ids=ids)
            self.logger.debug("Deleted %d documents from ChromaDB", len(ids))
            
        except Exception as e:
            self.logger.error("Failed to delete documents: %s", str(e))
            raise
    
    async def delete_by_filter(self, meta_filter: Dict[str, Any]):
        """
        Delete documents by metadata filter.
        
        Args:
            meta_filter: Metadata filter for deletion
        """
        try:
            chroma_where = self._convert_meta_filter_to_chroma(meta_filter)
            self.collection.delete(where=chroma_where)
            self.logger.debug("Deleted documents with filter: %s", meta_filter)
            
        except Exception as e:
            self.logger.error("Failed to delete documents by filter: %s", str(e))
            raise
    
    def _convert_meta_filter_to_chroma(self, meta_filter: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert MongoDB-style meta_filter to ChromaDB where clause.
        
        Args:
            meta_filter: MongoDB-style filter
            
        Returns:
            ChromaDB compatible where clause
        """
        if not meta_filter:
            return {}
        
        # Handle $and operator
        if "$and" in meta_filter:
            chroma_filter = {}
            for condition in meta_filter["$and"]:
                chroma_filter.update(self._convert_meta_filter_to_chroma(condition))
            return chroma_filter
        
        # Handle regular fields
        chroma_filter = {}
        for key, value in meta_filter.items():
            if isinstance(value, dict):
                # Handle range operators
                if "$gte" in value:
                    chroma_filter[key] = {"$gte": value["$gte"]}
                if "$lte" in value:
                    if key in chroma_filter:
                        chroma_filter[key].update({"$lte": value["$lte"]})
                    else:
                        chroma_filter[key] = {"$lte": value["$lte"]}
                if "$gt" in value:
                    chroma_filter[key] = {"$gt": value["$gt"]}
                if "$lt" in value:
                    if key in chroma_filter:
                        chroma_filter[key].update({"$lt": value["$lt"]})
                    else:
                        chroma_filter[key] = {"$lt": value["$lt"]}
                if "$eq" in value:
                    chroma_filter[key] = value["$eq"]
                if "$ne" in value:
                    chroma_filter[key] = {"$ne": value["$ne"]}
            else:
                # Simple equality
                chroma_filter[key] = value
        
        return chroma_filter
    
    def _build_keyword_query(self, keywords_filter: List[str]) -> Dict[str, Any]:
        """
        Build ChromaDB keyword query from keywords filter.
        
        Args:
            keywords_filter: List of keywords for OR query
            
        Returns:
            ChromaDB compatible where_document clause
        """
        if not keywords_filter:
            return {}
        
        if len(keywords_filter) == 1:
            # Single keyword - simple $contains
            return {"$contains": keywords_filter[0]}
        else:
            # Multiple keywords - always use $or
            return {"$or": [{"$contains": kw} for kw in keywords_filter if kw]}
    
    def get_collection_info(self) -> Dict[str, Any]:
        """
        Get information about the collection.
        
        Returns:
            Dictionary with collection information
        """
        try:
            count = self.collection.count()
            return {
                "name": self.collection.name,
                "count": count,
                "embedding_function": str(self.collection._embedding_function)
            }
        except Exception as e:
            self.logger.error("Failed to get collection info: %s", str(e))
            return {}
    
    def __repr__(self) -> str:
        """String representation of VectorStoreLocal instance."""
        try:
            info = self.get_collection_info()
            return f"VectorStoreLocal(collection='{info.get('name', 'unknown')}', count={info.get('count', 0)})"
        except:
            return f"VectorStoreLocal(collection='{self.collection.name}')"
