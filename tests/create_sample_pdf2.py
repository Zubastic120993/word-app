"""Create a second sample PDF for testing sessions."""

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from pathlib import Path


def create_sample_vocabulary_pdf2(output_path: Path) -> None:
    """Create a second sample vocabulary PDF with more Polish-English pairs."""
    
    c = canvas.Canvas(str(output_path), pagesize=letter)
    width, height = letter
    
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, height - 50, "Polish Vocabulary - Lesson 2")
    
    c.setFont("Helvetica", 12)
    
    vocabulary = [
        # More words
        "mleko - milk",
        "chleb - bread",
        "maslo - butter",
        "jajko - egg",
        "ser - cheese",
        "herbata - tea",
        "kawa - coffee",
        "sok - juice",
        
        # More phrases
        "Prosze bardzo - You're welcome",
        "Nie rozumiem - I don't understand",
        
        # More sentences
        "Gdzie jest toaleta? - Where is the toilet?",
        "Ile to kosztuje? - How much does this cost?",
    ]
    
    y_position = height - 100
    line_height = 25
    
    for line in vocabulary:
        c.drawString(50, y_position, line)
        y_position -= line_height
    
    c.save()
    print(f"Created sample PDF at: {output_path}")


if __name__ == "__main__":
    output = Path(__file__).parent.parent / "data" / "sample_vocabulary2.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    create_sample_vocabulary_pdf2(output)
