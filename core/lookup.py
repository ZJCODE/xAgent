import json
from dotenv import load_dotenv
import time

from langfuse import observe
from langfuse.openai import OpenAI

from schemas.vocabulary import BaseVocabularyRecord,VocabularyRecord
from db.vocabulary_db import VocabularyDB


load_dotenv(override=True)

class VocabularyService:
    def __init__(self,
                 model: str = "gpt-4o-mini",
                 db: VocabularyDB = None):
        self.client = OpenAI()
        self.db = db or VocabularyDB()
        self.model = model
        self.system_message = (
            "You are a helpful vocabulary assistant. "
            "When explaining a word, tailor your explanation, examples, and details according to the word's difficulty level: "
            "For 'Beginner', use simple language and basic examples. "
            "For 'Intermediate', provide more detail and moderately complex examples. "
            "For 'Advanced', give in-depth explanations and sophisticated example sentences. "
            "Always include the word's definition, example sentences, and specify the difficulty level in your response."
        )
    @observe()
    def lookup_word(self, word: str) -> BaseVocabularyRecord:
        response = self.client.responses.parse(
            model=self.model,
            input=[
                {"role": "system", "content": self.system_message},
                {"role": "user", "content": word}
                ],
            text_format=BaseVocabularyRecord,
        )
        record = response.output_parsed
        return record
    
    @observe()
    def save_vocabulary(self, user_id: str, record: BaseVocabularyRecord, **kwargs) -> bool:
        """
        Save or update a vocabulary record for a user.
        :param user_id: User ID
        :param record: VocabularyRecord instance
        :param kwargs: Additional fields to store in the record's extra field
        :return: True if successful
        """
        data = record.model_dump()
        data["user_id"] = user_id
        data["create_timestamp"] = time.time()
        if kwargs:
            data["extra"] = kwargs
        vocab_record = VocabularyRecord.model_validate(data)
        return self.db.save_vocabulary(vocab_record)


if __name__ == "__main__":
    service = VocabularyService()
    record = service.lookup_word("crunchy")
    print(json.dumps(record.model_dump(), ensure_ascii=False, indent=2))
    service.save_vocabulary(
        user_id="user123",
        record=record
    )
    service.save_vocabulary(
        user_id="user123_extra",
        record=record,
        part_of_speech="adjective",
        usage="common in describing food texture",
        image_url="http://example.com/crunchy.png"
    )