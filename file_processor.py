import fitz  # PyMuPDF
import docx


def extract_text_from_pdf(filepath):
    """Extract text from PDF using PyMuPDF"""
    try:
        with fitz.open(filepath) as doc:
            text = "".join(page.get_text() for page in doc)
        return text
    except Exception as e:
        print(f"Error reading PDF: {e}")
        return ""


def extract_text_from_docx(filepath):
    """Extract text from DOCX using python-docx"""
    try:
        doc = docx.Document(filepath)
        text = "\n".join(para.text for para in doc.paragraphs)
        return text
    except Exception as e:
        print(f"Error reading DOCX: {e}")
        return ""


def extract_text_from_txt(filepath):
    """Extract text from plain TXT file"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"Error reading TXT: {e}")
        return ""


# Optional: Dispatcher function
def extract_text(filepath):
    if filepath.lower().endswith(".pdf"):
        return extract_text_from_pdf(filepath)
    elif filepath.lower().endswith(".docx"):
        return extract_text_from_docx(filepath)
    elif filepath.lower().endswith(".txt"):
        return extract_text_from_txt(filepath)
    else:
        raise ValueError("Unsupported file type")
