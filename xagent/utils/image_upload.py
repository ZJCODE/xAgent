import os
import boto3
from botocore.exceptions import NoCredentialsError
from dotenv import load_dotenv
import uuid

load_dotenv(override=True)

s3 = boto3.client(
    's3',
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    region_name=os.getenv("AWS_REGION")
)

def upload_image(file_path: str) -> str:
    bucket = os.getenv("BUCKET_NAME")
    name = f"upload_{uuid.uuid4().hex[:8]}_{os.path.basename(file_path)}"
    try:
        s3.upload_file(file_path, bucket, name)
        return f"https://{bucket}.s3.amazonaws.com/{name}"
    except (FileNotFoundError, NoCredentialsError) as e:
        print("File not found or credentials error:", e)
        return None
    

if __name__ == "__main__":
    # Example usage of the upload_image function
    file_path = "tests/assets/test_image.png"  # Replace with your image file path
    uploaded_url = upload_image(file_path)
    if uploaded_url:
        print("Image uploaded successfully:", uploaded_url)
    else:
        print("Image upload failed.")