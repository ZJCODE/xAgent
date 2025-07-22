import os
import json
from dotenv import load_dotenv
import time

from langfuse import observe
from langfuse.openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from pydantic import ValidationError


from schemas.vocabulary import BaseVocabularyRecord,VocabularyRecord
from db.vocabulary_db import VocabularyDB


load_dotenv(override=True)

class VocabularyService:

    DEFAULT_USER_ID = "anonymous"

    DEFAULT_MODEL = "gpt-4o-mini"

    SYSTEM_MESSAGE = (
            "You are a helpful vocabulary assistant. "
            "When explaining a word, tailor your explanation, examples, and details according to the word's difficulty level: "
            "For 'Beginner', use simple language and basic examples. "
            "For 'Intermediate', provide more detail and moderately complex examples. "
            "For 'Advanced', give in-depth explanations and sophisticated example sentences. "
            "Always include the word's definition, example sentences, and specify the difficulty level in your response."
            )

    def __init__(self,
                 model: str = DEFAULT_MODEL,
                 client: OpenAI = None,
                 db: VocabularyDB = None,
                 ):

        self.client = client or OpenAI()
        self.model = model
        self.db = db or VocabularyDB(os.environ.get("REDIS_URL"))
        self.system_message = self.SYSTEM_MESSAGE
    
    @observe()
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def lookup_word(self, word: str, user_id: str = None, 
                    save: bool = False, cache: bool = True, **kwargs) -> BaseVocabularyRecord | VocabularyRecord:
        """
        Look up a word using the LLM. Optionally save the result to the database if save=True and user_id is provided.
        If save=True and the word already exists for the user, return the existing record from the database.
        :param word: Word to look up
        :param user_id: User ID (required if save=True)
        :param save: Whether to save the result to the database
        :param kwargs: Additional fields to store in the record's extra field if saving
        :return: BaseVocabularyRecord (or VocabularyRecord if saved)
        """
        if not user_id:
            user_id = self.DEFAULT_USER_ID
            save = True
        word = self._preprocess_word(word)
        if not word or not word.strip():
            raise ValueError("Word cannot be empty or None")
        if cache:
            existing = self.db.get_vocabulary(user_id, word)
            if existing:
                return existing
        record = self._llm_lookup_word(word)
        vocab_record = self._create_vocabulary_record(user_id, record, **kwargs)
        if save:
            self.db.save_vocabulary(vocab_record)
        return vocab_record
    
    @observe()
    def _llm_lookup_word(self, word: str) -> BaseVocabularyRecord:
        """
        Query the LLM to get the vocabulary record for the given word.
        :param word: Word to look up
        :return: BaseVocabularyRecord
        """
        completion = self.client.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_message},
                {"role": "user", "content": word}
            ],
            response_format=BaseVocabularyRecord,
        )
        return completion.choices[0].message.parsed
    
    def _create_vocabulary_record(self, user_id: str, record: BaseVocabularyRecord, **kwargs) -> VocabularyRecord:
        now = time.time()
        data = record.model_dump()
        data["user_id"] = user_id
        data["create_timestamp"] = now
        data["update_timestamp"] = now
        data["extra"] = kwargs if kwargs else {}
        try:
            return VocabularyRecord.model_validate(data)
        except ValidationError as e:
            print("ValidationError:", e)
            raise
    
    def _preprocess_word(self, word: str) -> str:
        """
        Preprocess the word for lookup, e.g., stripping whitespace.
        :param word: Word to preprocess
        :return: Preprocessed word or None if empty
        """
        if not word:
            return None
        w = word.strip().lower()
        return w if w else None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_vocabulary(self, user_id: str, word: str) -> VocabularyRecord:
        """
        Retrieve a vocabulary record for a specific user and word.
        :param user_id: User ID
        :param word: Word to look up
        :return: VocabularyRecord instance or None if not found
        """
        word = self._preprocess_word(word)
        return self.db.get_vocabulary(user_id, word)
    

if __name__ == "__main__":
    # Example usage
    service = VocabularyService()
    try:
        record = service.lookup_word("apple",cache=False)
        print(json.dumps(record.model_dump(), indent=2, ensure_ascii=False))
        record = service.lookup_word("apple", user_id="user123", save=True, part_of_speech="noun")
        print(json.dumps(record.model_dump(), indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}")