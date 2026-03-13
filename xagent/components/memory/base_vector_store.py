from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from pydantic import BaseModel

class VectorDoc(BaseModel):
    """Schema for a single vector document."""
    id: str
    document: str
    metadata: Dict[str, Any]
    score: Optional[float] = None # if is distance convert to similarity by using 1 - distance

class VectorStoreBase(ABC):
    """Abstract interface for vector storage operations."""

    @abstractmethod
    async def upsert(self,
                     ids: List[str],
                     documents: List[str],
                     metadatas: List[Dict[str, Any]]
                     ):
        """
        Upsert multiple vector pieces for a user.
        """
        pass

    
    @abstractmethod
    async def query(self,
                    query_texts: Optional[List[str]] = None,
                    n_results: Optional[int] = 5,
                    meta_filter: Optional[Dict[str, Any]] = None,
                    keywords_filter: Optional[List[str]] = None
                    ) -> List[VectorDoc]:
        """
        Query vector pieces for a user.
        
        Args:
            query_texts: List of query strings to search for
            n_results: Maximum number of results to return
            meta_filter: Metadata filter conditions (supports MongoDB-style queries):
                        - Simple: {"user_id": "user123"}
                        - Range: {"created_timestamp": {"$gte": 1234567, "$lte": 2345678}}
                        - And: {"$and": [{"user_id": "user123"}, {"created_timestamp": {"$gte": 1234567}}]}
            keywords_filter: List of keywords for document content filtering
                           Multiple keywords will be combined with OR logic
            
        Returns:
            List of VectorDoc objects sorted by relevance
        """
        pass

    @abstractmethod
    async def delete(self,
                     ids: List[str]
                     ):
        """
        Delete multiple vector pieces for a user.
        """
        pass