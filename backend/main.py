from fastapi import FastAPI
from supabase import create_client, Client
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# Supabase setup
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")
# supabase: Client = create_client(url, key) # Uncomment when keys are added

@app.get("/")
def read_root():
    return {"status": "Backend is alive"}