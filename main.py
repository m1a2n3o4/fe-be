import json
import os
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
from database import Base, engine, get_db

load_dotenv()

# Create tables on startup (simple setup — swap for Alembic migrations in production).
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Smart Feedback Desk")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-2.5-flash"

CATEGORIES = ["Bug", "Feature Request", "Praise", "Complaint", "Question", "Other"]


# ---------- Schemas ----------


class FeedbackCreate(BaseModel):
    customer: str
    message: str


class FeedbackOut(BaseModel):
    id: int
    customer: str
    message: str
    category: Optional[str] = None
    ai_reply: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ---------- Endpoints ----------


@app.get("/")
def root():
    return {"status": "ok", "service": "Smart Feedback Desk", "docs": "/docs"}


@app.get("/feedback", response_model=list[FeedbackOut])
def list_feedback(db: Session = Depends(get_db)):
    return db.query(models.Feedback).order_by(models.Feedback.created_at.desc()).all()


@app.post("/feedback", response_model=FeedbackOut, status_code=201)
def create_feedback(payload: FeedbackCreate, db: Session = Depends(get_db)):
    feedback = models.Feedback(customer=payload.customer, message=payload.message)
    db.add(feedback)
    db.commit()
    db.refresh(feedback)
    return feedback


@app.delete("/feedback/{feedback_id}", status_code=204)
def delete_feedback(feedback_id: int, db: Session = Depends(get_db)):
    feedback = db.get(models.Feedback, feedback_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail="Feedback not found")
    db.delete(feedback)
    db.commit()
    return None


@app.post("/feedback/{feedback_id}/analyze", response_model=FeedbackOut)
def analyze_feedback(feedback_id: int, db: Session = Depends(get_db)):
    feedback = db.get(models.Feedback, feedback_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail="Feedback not found")

    system = (
        "You are a customer support assistant for the Smart Feedback Desk. "
        "Read a piece of customer feedback, classify it into exactly one category, "
        "and write a short, warm, professional reply addressed to the customer."
    )
    prompt = (
        f"Customer: {feedback.customer}\n"
        f"Feedback: {feedback.message}\n\n"
        f"Choose the category from: {', '.join(CATEGORIES)}."
    )

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema={
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": CATEGORIES},
                        "reply": {"type": "string"},
                    },
                    "required": ["category", "reply"],
                },
            ),
        )
    except genai_errors.APIError as exc:
        raise HTTPException(status_code=502, detail=f"Gemini API error: {exc}") from exc

    text = response.text
    if not text:
        raise HTTPException(status_code=502, detail="Gemini returned no usable response")

    data = json.loads(text)
    feedback.category = data["category"]
    feedback.ai_reply = data["reply"]
    db.commit()
    db.refresh(feedback)
    return feedback
