import logging
import uuid
from typing import List, Optional, Dict, Any
import dotenv

from upstash_vector import Index, Vector

from .base_vector_store import VectorStoreBase, VectorDoc

dotenv.load_dotenv(override=True)


class VectorStoreUpstash(VectorStoreBase):
    """
    Upstash Vector storage implementation.
    
    This class provides Upstash Vector-based storage operations including
    upserting, querying, and deleting vector documents.
    
    Args:
        index: Optional pre-initialized Upstash Vector Index. If None, creates from environment
    """
    
    def __init__(self, index: Optional[Index] = None):
        # Initialize logger
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        
        # Initialize Upstash Vector index
        if index is not None:
            self.index = index
            self.logger.info("Using provided Upstash Vector index")
        else:
            try:
                self.index = Index.from_env()
                self.logger.info("Successfully initialized Upstash Vector index from environment")
            except Exception as e:
                self.logger.error("Failed to initialize Upstash Vector index: %s", str(e))
                raise
        
        self.logger.info("VectorStoreUpstash initialized successfully")
    
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
            # Prepare vectors for batch upsert
            vectors = []
            for i, (doc_id, document, metadata) in enumerate(zip(ids, documents, metadatas)):
                vector = Vector(
                    id=doc_id,
                    data=document,
                    metadata=metadata
                )
                vectors.append(vector)
            
            # Batch upsert
            self.index.upsert(vectors=vectors)
            self.logger.debug("Upserted %d documents to Upstash Vector", len(ids))
            
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
            keywords_filter: Do NOT use keywords filter (Upstash Vector does not support it)
            
        Returns:
            List of VectorDoc objects
        """
        if not query_texts:
            raise ValueError("query_texts must be provided for Upstash Vector queries")
        
        try:
            vector_docs = []
            
            # Process each query text (Upstash Vector processes queries individually)
            for query_text in query_texts:
                # Prepare query parameters
                query_params = {
                    "data": query_text,
                    "top_k": n_results or 5,
                    "include_vectors": False,
                    "include_metadata": True,
                    "include_data": True
                }
                
                # Convert meta_filter to Upstash filter format
                if meta_filter:
                    upstash_filter = self._convert_meta_filter_to_upstash(meta_filter)
                    query_params["filter"] = upstash_filter
                
                # Execute query
                results = self.index.query(**query_params)
                
                # Convert results to VectorDoc format
                for result in results:
                    # Upstash Vector returns similarity scores (higher is better)
                    score = result.score if hasattr(result, 'score') else None
                    
                    vector_doc = VectorDoc(
                        id=result.id,
                        document=result.data if hasattr(result, 'data') else "",
                        metadata=result.metadata if hasattr(result, 'metadata') else {},
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
            self.index.delete(ids=ids)
            self.logger.debug("Deleted %d documents from Upstash Vector", len(ids))
            
        except Exception as e:
            self.logger.error("Failed to delete documents: %s", str(e))
            raise
    
    async def delete_by_filter(self, meta_filter: Dict[str, Any]):
        """
        Delete documents by metadata filter.
        Note: This requires querying first then deleting by IDs in Upstash Vector.
        
        Args:
            meta_filter: Metadata filter for deletion
        """
        try:
            # Convert meta_filter to Upstash format
            upstash_filter = self._convert_meta_filter_to_upstash(meta_filter)
            
            # First, query to get IDs of documents matching the filter
            # We use a dummy query to get all matching documents
            query_params = {
                "data": "",  # Empty query to match based on filter only
                "top_k": 1000,  # Get many results to ensure we catch all
                "include_vectors": False,
                "include_metadata": True,
                "include_data": False,
                "filter": upstash_filter
            }
            
            try:
                results = self.index.query(**query_params)
                ids_to_delete = [result.id for result in results]
                
                if ids_to_delete:
                    await self.delete(ids_to_delete)
                    self.logger.debug("Deleted %d documents with filter: %s", len(ids_to_delete), meta_filter)
                else:
                    self.logger.debug("No documents found matching filter: %s", meta_filter)
                    
            except Exception as query_error:
                self.logger.warning("Filter-based deletion failed, trying alternative approach: %s", str(query_error))
                # Alternative: Try to delete using the filter directly if supported
                # This might not be supported in all Upstash Vector versions
                raise query_error
                
        except Exception as e:
            self.logger.error("Failed to delete documents by filter: %s", str(e))
            raise
    
    def _convert_meta_filter_to_upstash(self, meta_filter: Dict[str, Any]) -> str:
        """
        Convert MongoDB-style meta_filter to Upstash Vector filter string.
        
        Args:
            meta_filter: MongoDB-style filter
            
        Returns:
            Upstash Vector compatible filter string
        """
        if not meta_filter:
            return ""
        
        # Handle $and operator
        if "$and" in meta_filter:
            conditions = []
            for condition in meta_filter["$and"]:
                sub_filter = self._convert_meta_filter_to_upstash(condition)
                if sub_filter:
                    conditions.append(sub_filter)
            return " AND ".join(conditions)
        
        # Handle regular fields
        conditions = []
        for key, value in meta_filter.items():
            if isinstance(value, dict):
                # Handle range operators
                if "$gte" in value:
                    conditions.append(f"{key} >= {value['$gte']}")
                if "$lte" in value:
                    conditions.append(f"{key} <= {value['$lte']}")
                if "$gt" in value:
                    conditions.append(f"{key} > {value['$gt']}")
                if "$lt" in value:
                    conditions.append(f"{key} < {value['$lt']}")
                if "$eq" in value:
                    if isinstance(value["$eq"], str):
                        conditions.append(f"{key} = '{value['$eq']}'")
                    else:
                        conditions.append(f"{key} = {value['$eq']}")
                if "$ne" in value:
                    if isinstance(value["$ne"], str):
                        conditions.append(f"{key} != '{value['$ne']}'")
                    else:
                        conditions.append(f"{key} != {value['$ne']}")
            else:
                # Simple equality
                if isinstance(value, str):
                    conditions.append(f"{key} = '{value}'")
                else:
                    conditions.append(f"{key} = {value}")
        
        return " AND ".join(conditions)
    
    
    async def get_info(self) -> Dict[str, Any]:
        """
        Get information about the index.
        
        Returns:
            Dictionary with index information
        """
        try:
            info = self.index.info()
            return {
                "dimension": info.dimension if hasattr(info, 'dimension') else None,
                "total_vector_count": info.total_vector_count if hasattr(info, 'total_vector_count') else None,
                "similarity_function": info.similarity_function if hasattr(info, 'similarity_function') else None,
            }
        except Exception as e:
            self.logger.error("Failed to get index info: %s", str(e))
            return {}
    
    async def reset(self):
        """
        Reset the index (delete all vectors).
        Use with caution!
        """
        try:
            self.index.reset()
            self.logger.warning("Index has been reset - all vectors deleted!")
            
        except Exception as e:
            self.logger.error("Failed to reset index: %s", str(e))
            raise
    
    def __repr__(self) -> str:
        """String representation of VectorStoreUpstash instance."""
        try:
            # Try to get basic info for representation
            return f"VectorStoreUpstash(index=initialized)"
        except:
            return f"VectorStoreUpstash(index=unknown_state)"
