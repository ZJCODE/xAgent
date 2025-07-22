import os
import json
from dotenv import load_dotenv
import time

from langfuse import observe
from langfuse.openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from schemas.vocabulary import BaseVocabularyRecord,VocabularyRecord
from db.vocabulary_db import VocabularyDB


load_dotenv(override=True)

class VocabularyService:

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
    def lookup_word(self, word: str, user_id: str = None, save: bool = False, **kwargs) -> BaseVocabularyRecord:
        """
        Look up a word using the LLM. Optionally save the result to the database if save=True and user_id is provided.
        If save=True and the word already exists for the user, return the existing record from the database.
        :param word: Word to look up
        :param user_id: User ID (required if save=True)
        :param save: Whether to save the result to the database
        :param kwargs: Additional fields to store in the record's extra field if saving
        :return: BaseVocabularyRecord (or VocabularyRecord if saved)
        """
        if save and not user_id:
            raise ValueError("user_id is required when save=True to avoid anonymous data.")

        word = self._preprocess_word(word)

        if word is None:
            raise ValueError("Word cannot be empty or None")

        if user_id:
            existing = self.db.get_vocabulary(user_id, word)
            if existing:
                return existing

        # Responses do not support langfuse tracing for now
        # response = self.client.responses.parse(
        #     model=self.model,
        #     input=[
        #         {"role": "system", "content": self.system_message},
        #         {"role": "user", "content": word}
        #     ],
        #     text_format=BaseVocabularyRecord,
        # )
        # record = response.output_parsed

        completion = self.client.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_message},
                {"role": "user", "content": word}
            ],
            response_format=BaseVocabularyRecord,
        )

        record = completion.choices[0].message.parsed

        if save and user_id:
            vocab_record = self._transform_record(user_id, record, **kwargs)
            self.db.save_vocabulary(vocab_record)
            return vocab_record
        return record
    
    def _transform_record(self, user_id: str, record: BaseVocabularyRecord, **kwargs) -> VocabularyRecord:
        """
        Transform a BaseVocabularyRecord into a VocabularyRecord with additional user-specific fields.
        :param user_id: User ID
        :param record: BaseVocabularyRecord instance
        :param kwargs: Additional fields to store in the record's extra field
        :return: VocabularyRecord instance
        """
        now = time.time()
        data = record.model_dump()
        data["user_id"] = user_id
        data["create_timestamp"] = now
        data["update_timestamp"] = now
        data["extra"] = kwargs if kwargs else {}
        return VocabularyRecord.model_validate(data)
    
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

    @observe()
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
    service = VocabularyService()
    record = service.lookup_word("instance")
    print(json.dumps(record.model_dump(), ensure_ascii=False, indent=2))
    record = service.lookup_word("instance", user_id="user123", save=True, tag="test")
    print(json.dumps(record.model_dump(), ensure_ascii=False, indent=2))
    record = service.lookup_word("Instance", user_id="user123")
    print(json.dumps(record.model_dump(), ensure_ascii=False, indent=2))