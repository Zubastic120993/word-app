"""Create a sample PDF for testing the vocabulary app."""

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pathlib import Path


def create_sample_vocabulary_pdf(output_path: Path) -> None:
    """Create a sample vocabulary PDF with Polish-English pairs."""
    
    c = canvas.Canvas(str(output_path), pagesize=letter)
    width, height = letter
    
    # Title
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, height - 50, "Polish Vocabulary - Lesson 1")
    
    # Vocabulary lines
    c.setFont("Helvetica", 12)
    
    vocabulary = [
        # Words
        "cukier (noun) - sugar",
        "dom - house",
        "kot - cat",
        "pies - dog",
        "woda - water",
        
        # Phrases
        "Co to jest - What is this",
        "Jak sie masz - How are you",
        "Dzien dobry - Good day",
        "Do widzenia - Goodbye",
        
        # Sentences
        "Jestem z Polski. - I am from Poland.",
        "Co to jest? - What is this?",
        "Mam na imie Jan. - My name is Jan.",
        "Dziekuje bardzo! - Thank you very much!",
    ]
    
    y_position = height - 100
    line_height = 25
    
    for line in vocabulary:
        c.drawString(50, y_position, line)
        y_position -= line_height
    
    c.save()
    print(f"Created sample PDF at: {output_path}")


if __name__ == "__main__":
    output = Path(__file__).parent.parent / "data" / "sample_vocabulary.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    create_sample_vocabulary_pdf(output)
