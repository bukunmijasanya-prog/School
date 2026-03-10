"""
Batch Processing for Nursery Extension
Analyses multiple children's assessment sheets in a single API call
"""

import openai
import json
import base64
from datetime import datetime


# Observation labels for converting checkboxes to readable text
VERBAL_LABELS = {
    'said_writing': "Said 'I'm writing' during writing tasks",
    'said_drawing': "Said 'I'm drawing' during drawing tasks",
    'named_letters': "Named letters or sounds while writing",
    'described_drawing': "Described their drawing while working",
    'said_cant': "Said 'I can't do it'",
    'no_comments': "Was quiet throughout"
}

PAPER_LABELS = {
    'turned_paper': "Turned/rotated the paper during tasks",
    'kept_still': "Kept paper still throughout"
}

DIFFICULTY_LABELS = {
    'confident': "Confident throughout all tasks",
    'needed_encouragement_writing': "Needed encouragement for writing tasks",
    'needed_encouragement_drawing': "Needed encouragement for drawing tasks",
    'hesitant': "Hesitant to start",
    'enjoyed': "Clearly enjoyed the tasks",
    'writing_harder': "Found writing harder than drawing",
    'drawing_harder': "Found drawing harder than writing"
}


SYSTEM_PROMPT = """You are an expert in early childhood development, specialising in writing and drawing differentiation in children aged 2-4 years.

You will analyse multiple children's assessment sheets. Each sheet is A4 landscape divided into 4 sections:
- Section 1 (top-left): Name writing - child asked to "write your name"
- Section 2 (top-right): Self portrait - child asked to "draw yourself"
- Section 3 (bottom-left): Sun writing - child asked to "write the word sun"
- Section 4 (bottom-right): Sun drawing - child asked to "draw a sun"

There are small numbers (1, 2, 3, 4) in the MARGINS outside the boxes - ignore these, they are for teacher reference only. Focus only on what is INSIDE each box.

For each child, compare the WRITING sections (1, 3) to the DRAWING sections (2, 4) to assess whether the child understands that writing and drawing are different activities.

ALSO consider any TEACHER OBSERVATIONS provided - these tell you things you cannot see in the images (what the child said, how they approached the tasks, whether they needed encouragement).

SCORING GUIDE:
- Score 1 (Pre-differentiation): Writing and drawing look identical; same marks for both
- Score 2 (Early emerging): Slight differences but inconsistent
- Score 3 (Emerging): Clear attempts to differentiate; writing may have letter-like forms
- Score 4 (Developing): Consistent differentiation; writing shows linearity, drawing shows enclosure
- Score 5 (Established): Clear distinction; writing attempts actual letters

Respond with JSON only, no markdown:
{
    "children": [
        {
            "name": "Child Name",
            "score": 3,
            "stage": "emerging",
            "writing_observations": "What you see in their writing sections...",
            "drawing_observations": "What you see in their drawing sections...",
            "differentiation_notes": "How their writing differs from drawing...",
            "recommendations": ["Activity suggestion 1", "Activity suggestion 2"]
        }
    ],
    "class_summary": {
        "total_children": 5,
        "average_score": 2.8,
        "stage_distribution": {
            "pre-differentiation": 1,
            "emerging": 2,
            "developing": 2,
            "established": 0
        },
        "common_patterns": "What you noticed across the class...",
        "class_recommendations": ["Suggestion for whole class"]
    }
}"""


def format_observations(verbal: list, paper: list, difficulty: list) -> str:
    """Convert observation checkbox values to readable text"""
    
    texts = []
    
    for key in (verbal or []):
        if key in VERBAL_LABELS:
            texts.append(VERBAL_LABELS[key])
    
    for key in (paper or []):
        if key in PAPER_LABELS:
            texts.append(PAPER_LABELS[key])
            
    for key in (difficulty or []):
        if key in DIFFICULTY_LABELS:
            texts.append(DIFFICULTY_LABELS[key])
    
    return "; ".join(texts) if texts else "None recorded"


def analyse_batch(children_data: list, api_key: str) -> dict:
    """
    Analyse multiple children's assessment sheets in one API call.
    
    Args:
        children_data: List of dicts with:
            - name: Child's name
            - age: Age string (e.g., "3 years 4 months")
            - image_base64: Base64 encoded assessment sheet image
            - observations_verbal: List of verbal observation keys
            - observations_paper: List of paper observation keys
            - observations_difficulty: List of difficulty observation keys
        api_key: OpenAI API key
    
    Returns:
        dict with analysis results for all children
    """
    
    client = openai.OpenAI(api_key=api_key)
    
    # Build the message content
    user_content = []
    
    # Introduction
    user_content.append({
        "type": "text",
        "text": f"Please analyse the following {len(children_data)} children's assessment sheets."
    })
    
    # Add each child's data
    for child in children_data:
        # Child header with observations
        observations_text = format_observations(
            child.get('observations_verbal', []),
            child.get('observations_paper', []),
            child.get('observations_difficulty', [])
        )
        
        header = f"""
--- {child['name'].upper()} (Age: {child['age']}) ---

TEACHER OBSERVATIONS: {observations_text}

ASSESSMENT SHEET:"""
        
        user_content.append({
            "type": "text",
            "text": header
        })
        
        # Add the image
        user_content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{child['image_base64']}",
                "detail": "high"
            }
        })
    
    # Make the API call
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=4000,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content}
        ]
    )
    
    # Parse the response
    result_text = response.choices[0].message.content
    
    # Clean up markdown if present
    if result_text.startswith("```"):
        result_text = result_text.strip("```json").strip("```").strip()
    
    result = json.loads(result_text)
    
    # Add usage info
    result['api_usage'] = {
        'prompt_tokens': response.usage.prompt_tokens,
        'completion_tokens': response.usage.completion_tokens,
        'total_tokens': response.usage.total_tokens
    }
    
    return result


def analyse_single(child_data: dict, api_key: str) -> dict:
    """
    Analyse a single child's assessment sheet.
    Wrapper around analyse_batch for single child.
    """
    
    result = analyse_batch([child_data], api_key)
    
    if result.get('children') and len(result['children']) > 0:
        return result['children'][0]
    
    return None


def split_into_batches(children: list, max_per_batch: int = 10) -> list:
    """
    Split a list of children into smaller batches.
    
    Args:
        children: List of children to process
        max_per_batch: Maximum children per batch (default 10)
    
    Returns:
        List of batches (each batch is a list of children)
    """
    
    batches = []
    for i in range(0, len(children), max_per_batch):
        batches.append(children[i:i + max_per_batch])
    
    return batches


def merge_batch_results(batch_results: list) -> dict:
    """
    Merge results from multiple batches into a single result.
    
    Args:
        batch_results: List of result dicts from analyse_batch
    
    Returns:
        Combined result dict
    """
    
    all_children = []
    total_tokens = 0
    
    for result in batch_results:
        all_children.extend(result.get('children', []))
        if 'api_usage' in result:
            total_tokens += result['api_usage'].get('total_tokens', 0)
    
    # Calculate combined summary
    total = len(all_children)
    if total == 0:
        return {'children': [], 'class_summary': None}
    
    total_score = sum(c.get('score', 0) for c in all_children)
    
    stages = {
        'pre-differentiation': 0,
        'emerging': 0,
        'developing': 0,
        'established': 0
    }
    
    for child in all_children:
        stage = child.get('stage', '').lower().replace(' ', '-')
        if stage in stages:
            stages[stage] += 1
    
    return {
        'children': all_children,
        'class_summary': {
            'total_children': total,
            'average_score': round(total_score / total, 2),
            'stage_distribution': stages
        },
        'api_usage': {
            'total_tokens': total_tokens
        }
    }
