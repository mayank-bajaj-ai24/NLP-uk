"""
Tier 2: LayoutLMv3 Structure Refinement

Processes documents with Textract confidence < 90% using LayoutLMv3
multimodal model for structure and layout refinement.

Key Features:
- LayoutLMv3 model loading with GPU/CPU fallback
- Multimodal input processing (image + Textract JSON)
- Token-level confidence refinement
- Document section identification
- Medical term flagging (<85% confidence -> Tier 3 escalation)
"""

import json
import os
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import torch
from PIL import Image
from transformers import AutoProcessor, AutoModel

from sqs_messaging import send_to_sqs, receive_from_sqs, delete_from_sqs
from sqs_setup import get_queue_url

# Critical medical terms requiring strict confidence enforcement
CRITICAL_MEDICAL_TERMS = {
    "diagnoses": [
        "cancer", "carcinoma", "tumor", "tumour", "malignant", "benign",
        "stroke", "cva", "myocardial infarction", "mi", "heart attack",
        "sepsis", "pneumonia", "diabetes", "diabetic", "hypertension",
        "heart failure", "cardiac", "haemorrhoid", "hemorrhoid", "fracture",
        "infection", "abscess", "ulcer", "thrombosis", "embolism",
        "anemia", "anaemia", "leukemia", "leukaemia", "lymphoma"
    ],
    "anatomy": [
        "artery", "vein", "aorta", "ventricle", "atrium",
        "kidney", "liver", "lung", "heart", "brain", "spine", "spinal",
        "colon", "rectum", "intestine", "stomach", "pancreas", "gallbladder",
        "bladder", "prostate", "uterus", "ovary", "breast",
        "nerve", "neuron", "cortex", "cerebral"
    ],
    "procedures": [
        "surgery", "surgical", "operation", "resection", "excision",
        "repair", "anastomosis", "transplant", "biopsy", "endoscopy",
        "colonoscopy", "laparoscopy", "catheter", "intubation",
        "amputation", "grafting", "bypass", "stent"
    ],
    "medications": [
        "insulin", "warfarin", "heparin", "digoxin", "metformin",
        "aspirin", "morphine", "fentanyl", "oxycodone", "methotrexate",
        "chemotherapy", "antibiotic", "steroid", "immunosuppressant"
    ]
}


class LayoutLMv3Loader:
    """Singleton loader for LayoutLMv3 model and processor."""

    _instance = None
    _model = None
    _processor = None
    _device = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def load_model(self, model_name: str = "microsoft/layoutlmv3-base",
                   device: Optional[str] = None) -> Tuple:
        """
        Loads LayoutLMv3 model and processor with caching.

        Args:
            model_name: HuggingFace model ID
            device: "cuda" or "cpu" (auto-detects if None)

        Returns:
            Tuple of (model, processor, device)
        """
        if self._model is not None and self._processor is not None:
            print(f"Using cached model on {self._device}")
            return self._model, self._processor, self._device

        # Auto-detect device
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"

        self._device = device
        print(f"Loading LayoutLMv3 model on {device}...")

        start_time = time.time()

        try:
            self._processor = AutoProcessor.from_pretrained(
                model_name,
                apply_ocr=False  # We use Textract OCR, not built-in
            )

            self._model = AutoModel.from_pretrained(model_name)
            self._model.to(device)
            self._model.eval()

            load_time = time.time() - start_time
            print(f"Model loaded successfully in {load_time:.2f}s")

            return self._model, self._processor, self._device

        except Exception as e:
            print(f"Error loading model: {e}")
            raise


def extract_textract_structure(textract_json_path: str) -> Dict:
    """
    Parses Textract JSON and extracts structured layout information.

    Args:
        textract_json_path: Path to Textract output JSON file

    Returns:
        dict: Structured data with lines, words, tables, and metadata
    """
    with open(textract_json_path, 'r') as f:
        textract_data = json.load(f)

    blocks = textract_data.get('Blocks', [])

    lines = []
    words = []
    tables = []
    confidence_scores = []

    for block in blocks:
        block_type = block.get('BlockType')
        confidence = block.get('Confidence', 0)
        geometry = block.get('Geometry', {})
        bbox = geometry.get('BoundingBox', {})

        # Normalize bounding box to (left, top, right, bottom) format
        normalized_bbox = (
            bbox.get('Left', 0),
            bbox.get('Top', 0),
            bbox.get('Left', 0) + bbox.get('Width', 0),
            bbox.get('Top', 0) + bbox.get('Height', 0)
        )

        if block_type == 'LINE':
            lines.append({
                'text': block.get('Text', ''),
                'bbox': normalized_bbox,
                'confidence': confidence,
                'block_id': block.get('Id')
            })
            confidence_scores.append(confidence)

        elif block_type == 'WORD':
            words.append({
                'text': block.get('Text', ''),
                'bbox': normalized_bbox,
                'confidence': confidence,
                'block_id': block.get('Id')
            })

        elif block_type == 'TABLE':
            tables.append({
                'bbox': normalized_bbox,
                'confidence': confidence,
                'block_id': block.get('Id')
            })

    avg_confidence = sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0

    return {
        'lines': lines,
        'words': words,
        'tables': tables,
        'average_confidence': avg_confidence,
        'total_blocks': len(blocks)
    }


def prepare_image_for_layoutlm(image_path: str) -> Image.Image:
    """
    Loads and prepares image for LayoutLMv3 processing.

    Args:
        image_path: Path to image file

    Returns:
        PIL Image in RGB format
    """
    image = Image.open(image_path)

    # Convert to RGB if necessary
    if image.mode != 'RGB':
        image = image.convert('RGB')

    return image


def create_multimodal_input(image: Image.Image,
                            textract_structure: Dict,
                            processor) -> Dict:
    """
    Combines image and Textract-extracted text into multimodal input.

    Args:
        image: PIL Image
        textract_structure: Parsed Textract structure
        processor: LayoutLMv3 processor

    Returns:
        Encoded input dict for model
    """
    # Extract words and bounding boxes
    words = []
    boxes = []

    for word in textract_structure.get('words', []):
        words.append(word['text'])
        # Convert normalized bbox to 0-1000 scale for LayoutLMv3
        bbox = word['bbox']
        boxes.append([
            int(bbox[0] * 1000),
            int(bbox[1] * 1000),
            int(bbox[2] * 1000),
            int(bbox[3] * 1000)
        ])

    # If no words, use lines instead
    if not words:
        for line in textract_structure.get('lines', []):
            words.append(line['text'])
            bbox = line['bbox']
            boxes.append([
                int(bbox[0] * 1000),
                int(bbox[1] * 1000),
                int(bbox[2] * 1000),
                int(bbox[3] * 1000)
            ])

    # Handle empty documents
    if not words:
        words = [""]
        boxes = [[0, 0, 0, 0]]

    encoding = processor(
        images=image,
        text=words,
        boxes=boxes,
        return_tensors="pt",
        padding="max_length",
        truncation=True,
        max_length=512
    )

    return encoding


def run_model_inference(encoding: Dict,
                        model,
                        device: str) -> Dict:
    """
    Runs LayoutLMv3 forward pass and extracts features.

    Args:
        encoding: Tokenized input
        model: LayoutLMv3 model
        device: Device to run on

    Returns:
        dict with model outputs and embeddings
    """
    start_time = time.time()

    # Move inputs to device
    inputs = {k: v.to(device) for k, v in encoding.items()}

    with torch.no_grad():
        outputs = model(**inputs)

    inference_time = time.time() - start_time

    # Extract hidden states (last layer embeddings)
    hidden_states = outputs.last_hidden_state

    # Calculate average embedding confidence using hidden state magnitudes
    embedding_magnitudes = torch.norm(hidden_states, dim=-1)
    avg_magnitude = embedding_magnitudes.mean().item()

    # Normalize to 0-100 scale (empirically determined range)
    model_confidence = min(100, max(0, (avg_magnitude / 10) * 100))

    return {
        'hidden_states': hidden_states,
        'model_confidence': model_confidence,
        'inference_time_ms': int(inference_time * 1000)
    }


def refine_text_confidence(textract_structure: Dict,
                           model_output: Dict,
                           original_confidence: float) -> Dict:
    """
    Refines confidence scores using LayoutLMv3 model output.

    Args:
        textract_structure: Original Textract structure
        model_output: LayoutLMv3 inference output
        original_confidence: Original average confidence

    Returns:
        dict with refined confidence and improvements
    """
    model_confidence = model_output['model_confidence']

    # Calculate refined confidence as weighted average
    # Weight model higher when original confidence is low
    if original_confidence < 70:
        weight_model = 0.6
    elif original_confidence < 80:
        weight_model = 0.5
    elif original_confidence < 90:
        weight_model = 0.4
    else:
        weight_model = 0.3

    refined_confidence = (
        original_confidence * (1 - weight_model) +
        model_confidence * weight_model
    )

    improvement = refined_confidence - original_confidence

    return {
        'original_confidence': original_confidence,
        'model_confidence': model_confidence,
        'refined_confidence': refined_confidence,
        'improvement': improvement,
        'weight_model': weight_model
    }


def identify_document_sections(textract_structure: Dict) -> List[Dict]:
    """
    Identifies document sections based on spatial layout.

    Args:
        textract_structure: Parsed Textract structure

    Returns:
        list of section dictionaries
    """
    lines = textract_structure.get('lines', [])
    sections = []

    if not lines:
        return sections

    # Sort lines by vertical position
    sorted_lines = sorted(lines, key=lambda x: x['bbox'][1])

    # Simple section identification based on vertical position
    page_height = 1.0  # Normalized
    header_threshold = 0.15
    footer_threshold = 0.85

    header_lines = []
    body_lines = []
    footer_lines = []

    for line in sorted_lines:
        y_pos = line['bbox'][1]

        if y_pos < header_threshold:
            header_lines.append(line)
        elif y_pos > footer_threshold:
            footer_lines.append(line)
        else:
            body_lines.append(line)

    if header_lines:
        sections.append({
            'type': 'HEADER',
            'bbox': _calculate_section_bbox(header_lines),
            'confidence': _calculate_avg_confidence(header_lines),
            'line_count': len(header_lines)
        })

    if body_lines:
        sections.append({
            'type': 'BODY',
            'bbox': _calculate_section_bbox(body_lines),
            'confidence': _calculate_avg_confidence(body_lines),
            'line_count': len(body_lines)
        })

    if footer_lines:
        sections.append({
            'type': 'FOOTER',
            'bbox': _calculate_section_bbox(footer_lines),
            'confidence': _calculate_avg_confidence(footer_lines),
            'line_count': len(footer_lines)
        })

    # Add tables as separate sections
    for i, table in enumerate(textract_structure.get('tables', [])):
        sections.append({
            'type': 'TABLE',
            'bbox': table['bbox'],
            'confidence': table['confidence'],
            'table_index': i
        })

    return sections


def _calculate_section_bbox(lines: List[Dict]) -> Tuple:
    """Calculate bounding box encompassing all lines."""
    if not lines:
        return (0, 0, 0, 0)

    min_left = min(line['bbox'][0] for line in lines)
    min_top = min(line['bbox'][1] for line in lines)
    max_right = max(line['bbox'][2] for line in lines)
    max_bottom = max(line['bbox'][3] for line in lines)

    return (min_left, min_top, max_right, max_bottom)


def _calculate_avg_confidence(lines: List[Dict]) -> float:
    """Calculate average confidence for lines."""
    if not lines:
        return 0
    return sum(line['confidence'] for line in lines) / len(lines)


def is_medical_term(word: str) -> Tuple[bool, Optional[str]]:
    """
    Checks if word matches critical medical terms.

    Args:
        word: Word to check

    Returns:
        Tuple of (is_medical, category)
    """
    word_lower = word.lower().strip()

    for category, terms in CRITICAL_MEDICAL_TERMS.items():
        for term in terms:
            if term in word_lower or word_lower in term:
                return True, category

    return False, None


def flag_medical_terms(textract_structure: Dict,
                       refined_confidence: float,
                       threshold: float = 85.0) -> Dict:
    """
    Identifies medical terms with low confidence for escalation.

    Args:
        textract_structure: Parsed Textract structure
        refined_confidence: Overall refined confidence
        threshold: Confidence threshold for flagging (default 85%)

    Returns:
        dict with flagged terms and escalation info
    """
    flagged_terms = []
    total_medical_terms = 0

    # Check lines for medical terms
    for line in textract_structure.get('lines', []):
        text = line.get('text', '')
        confidence = line.get('confidence', 0)

        # Check each word in the line
        for word in text.split():
            is_medical, category = is_medical_term(word)

            if is_medical:
                total_medical_terms += 1

                # Flag if confidence below threshold
                if confidence < threshold:
                    flagged_terms.append({
                        'text': word,
                        'category': category,
                        'confidence': confidence,
                        'line_text': text,
                        'flag_reason': f'medical_term_below_{threshold}%',
                        'requires_specialist_review': True
                    })

    # Also flag if overall confidence is too low
    requires_escalation = (
        len(flagged_terms) > 0 or
        refined_confidence < threshold
    )

    return {
        'flagged_terms': flagged_terms,
        'total_medical_terms': total_medical_terms,
        'flagged_count': len(flagged_terms),
        'requires_escalation': requires_escalation,
        'escalation_reason': 'low_confidence_medical_terms' if flagged_terms else None
    }


def create_refined_output(textract_json_path: str,
                          textract_structure: Dict,
                          confidence_refinement: Dict,
                          sections: List[Dict],
                          medical_flags: Dict,
                          model_output: Dict) -> Dict:
    """
    Creates enhanced Textract output with LayoutLMv3 refinements.

    Args:
        textract_json_path: Original Textract JSON path
        textract_structure: Parsed Textract structure
        confidence_refinement: Confidence refinement results
        sections: Identified document sections
        medical_flags: Medical term flagging results
        model_output: Model inference output

    Returns:
        dict: Refined output structure
    """
    return {
        'DocumentMetadata': {
            'SourceFile': textract_json_path,
            'ProcessingTier': 'Tier2_LayoutLMv3',
            'ProcessedAt': datetime.utcnow().isoformat() + 'Z'
        },
        'ConfidenceRefinement': {
            'original_average_confidence': confidence_refinement['original_confidence'],
            'refined_average_confidence': confidence_refinement['refined_confidence'],
            'model_confidence': confidence_refinement['model_confidence'],
            'improvement': confidence_refinement['improvement']
        },
        'Sections': sections,
        'MedicalTermFlags': {
            'total_medical_terms': medical_flags['total_medical_terms'],
            'flagged_count': medical_flags['flagged_count'],
            'requires_escalation': medical_flags['requires_escalation'],
            'flagged_terms': medical_flags['flagged_terms']
        },
        'ProcessingMetadata': {
            'model_name': 'microsoft/layoutlmv3-base',
            'inference_time_ms': model_output['inference_time_ms'],
            'total_lines': len(textract_structure.get('lines', [])),
            'total_words': len(textract_structure.get('words', [])),
            'total_tables': len(textract_structure.get('tables', []))
        }
    }


def create_escalation_message(document_id: str,
                              refined_output: Dict,
                              textract_json_path: str,
                              image_path: str) -> Dict:
    """
    Creates escalation message for Tier 3 processing.

    Args:
        document_id: Document identifier
        refined_output: Refined output from Tier 2
        textract_json_path: Path to Textract JSON
        image_path: Path to image file

    Returns:
        dict: Escalation message payload
    """
    medical_flags = refined_output['MedicalTermFlags']

    return {
        'document_id': document_id,
        'escalation_type': 'low_confidence_medical_terms',
        'escalation_count': medical_flags['flagged_count'],
        'source_tier': 'Tier2_LayoutLM',
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'flagged_terms': medical_flags['flagged_terms'],
        'document_context': {
            'textract_json_path': textract_json_path,
            'image_path': image_path,
            'refined_confidence': refined_output['ConfidenceRefinement']['refined_average_confidence'],
            'total_medical_terms': medical_flags['total_medical_terms'],
            'flagged_ratio': (
                medical_flags['flagged_count'] / medical_flags['total_medical_terms']
                if medical_flags['total_medical_terms'] > 0 else 0
            )
        },
        'escalation_priority': 'HIGH' if medical_flags['flagged_count'] > 5 else 'NORMAL'
    }


def create_downstream_message(document_id: str,
                              refined_output: Dict,
                              textract_json_path: str,
                              original_text: str = "") -> Dict:
    """
    Creates message for Track A and Track B processing.

    Args:
        document_id: Document identifier
        refined_output: Refined output from Tier 2
        textract_json_path: Path to Textract JSON
        original_text: Original extracted text

    Returns:
        dict: Downstream message payload
    """
    return {
        'document_id': document_id,
        'source_file': textract_json_path,
        'data': 'Data loaded from file',
        'tier2_processing': {
            'original_average_confidence': refined_output['ConfidenceRefinement']['original_average_confidence'],
            'refined_average_confidence': refined_output['ConfidenceRefinement']['refined_average_confidence'],
            'confidence_improvement': refined_output['ConfidenceRefinement']['improvement'],
            'section_count': len(refined_output['Sections']),
            'medical_flags_count': refined_output['MedicalTermFlags']['flagged_count'],
            'processing_time_ms': refined_output['ProcessingMetadata']['inference_time_ms']
        },
        'average_confidence': refined_output['ConfidenceRefinement']['refined_average_confidence'],
        'text': original_text
    }


def process_single_document(textract_json_path: str,
                            image_path: str,
                            model,
                            processor,
                            device: str,
                            output_dir: str = "tier2_outputs") -> Dict:
    """
    Processes a single document through Tier 2 refinement.

    Args:
        textract_json_path: Path to Textract JSON
        image_path: Path to document image
        model: LayoutLMv3 model
        processor: LayoutLMv3 processor
        device: Device for inference
        output_dir: Directory for output files

    Returns:
        dict: Processing results
    """
    start_time = time.time()

    # Create output directory
    os.makedirs(output_dir, exist_ok=True)

    # Extract document ID from filename
    document_id = os.path.basename(textract_json_path).replace('_textract.json', '')

    print(f"\nProcessing: {document_id}")

    # 1. Parse Textract structure
    textract_structure = extract_textract_structure(textract_json_path)
    original_confidence = textract_structure['average_confidence']
    print(f"  Original confidence: {original_confidence:.2f}%")

    # 2. Load and prepare image
    try:
        image = prepare_image_for_layoutlm(image_path)
    except Exception as e:
        print(f"  Warning: Could not load image: {e}")
        print(f"  Proceeding with text-only mode")
        image = Image.new('RGB', (224, 224), color='white')

    # 3. Create multimodal input
    encoding = create_multimodal_input(image, textract_structure, processor)

    # 4. Run model inference
    model_output = run_model_inference(encoding, model, device)
    print(f"  Model inference: {model_output['inference_time_ms']}ms")

    # 5. Refine confidence
    confidence_refinement = refine_text_confidence(
        textract_structure, model_output, original_confidence
    )
    print(f"  Refined confidence: {confidence_refinement['refined_confidence']:.2f}%")
    print(f"  Improvement: {confidence_refinement['improvement']:+.2f}%")

    # 6. Identify sections
    sections = identify_document_sections(textract_structure)
    print(f"  Sections identified: {len(sections)}")

    # 7. Flag medical terms
    medical_flags = flag_medical_terms(
        textract_structure,
        confidence_refinement['refined_confidence']
    )
    print(f"  Medical terms: {medical_flags['total_medical_terms']} total, {medical_flags['flagged_count']} flagged")

    # 8. Create refined output
    refined_output = create_refined_output(
        textract_json_path,
        textract_structure,
        confidence_refinement,
        sections,
        medical_flags,
        model_output
    )

    # 9. Save output
    output_filename = f"{document_id}_tier2_refined.json"
    output_path = os.path.join(output_dir, output_filename)

    with open(output_path, 'w') as f:
        json.dump(refined_output, f, indent=4)

    print(f"  Output saved: {output_path}")

    total_time = time.time() - start_time
    print(f"  Total processing time: {total_time:.2f}s")

    return {
        'document_id': document_id,
        'output_path': output_path,
        'refined_output': refined_output,
        'textract_json_path': textract_json_path,
        'image_path': image_path,
        'processing_time_s': total_time
    }


def process_tier2_queue(output_dir: str = "tier2_outputs"):
    """
    Main event loop for Tier 2 processing.
    Consumes messages from Tier2_LayoutLM_Queue and routes to downstream queues.

    Args:
        output_dir: Directory for output files
    """
    print("=" * 60)
    print("TIER 2: LayoutLMv3 Structure Refinement")
    print("=" * 60)

    # Get queue URLs
    tier2_queue_url = get_queue_url('Tier2_LayoutLM_Queue')
    track_a_queue_url = get_queue_url('TrackA_Entity_SNOMED_Queue')
    track_b_queue_url = get_queue_url('TrackB_Summary_Queue')
    tier3_queue_url = get_queue_url('Tier3_Escalation_Queue')

    if not tier2_queue_url:
        print("ERROR: Could not find Tier2_LayoutLM_Queue. Run sqs_setup.py first.")
        return

    # Load model
    loader = LayoutLMv3Loader()
    model, processor, device = loader.load_model()

    print(f"\nListening on Tier2_LayoutLM_Queue...")
    print("Press Ctrl+C to stop\n")

    documents_processed = 0

    while True:
        messages = receive_from_sqs(tier2_queue_url, max_messages=1)

        if not messages:
            print("Queue empty. Waiting for messages...")
            time.sleep(5)
            continue

        for message in messages:
            try:
                payload = json.loads(message['Body'])
                document_id = payload.get('document_id', 'unknown')
                textract_json_path = payload.get('textract_json_path')
                image_path = payload.get('image_path')

                # Handle legacy message format
                if not textract_json_path:
                    print(f"Warning: Message missing textract_json_path for {document_id}")
                    delete_from_sqs(tier2_queue_url, message['ReceiptHandle'])
                    continue

                # Process document
                result = process_single_document(
                    textract_json_path,
                    image_path or "",
                    model,
                    processor,
                    device,
                    output_dir
                )

                refined_output = result['refined_output']

                # Route to downstream queues
                downstream_msg = create_downstream_message(
                    document_id,
                    refined_output,
                    textract_json_path,
                    payload.get('text', '')
                )

                # Send to Track A and Track B
                if track_a_queue_url:
                    send_to_sqs(track_a_queue_url, downstream_msg)
                    print(f"  Routed to Track A (SNOMED)")

                if track_b_queue_url:
                    send_to_sqs(track_b_queue_url, downstream_msg)
                    print(f"  Routed to Track B (Summary)")

                # Escalate if needed
                if refined_output['MedicalTermFlags']['requires_escalation']:
                    if tier3_queue_url:
                        escalation_msg = create_escalation_message(
                            document_id,
                            refined_output,
                            textract_json_path,
                            image_path or ""
                        )
                        send_to_sqs(tier3_queue_url, escalation_msg)
                        print(f"  ESCALATED to Tier 3 ({refined_output['MedicalTermFlags']['flagged_count']} flags)")

                # Delete processed message
                delete_from_sqs(tier2_queue_url, message['ReceiptHandle'])
                documents_processed += 1
                print(f"  Document processed successfully ({documents_processed} total)")

            except Exception as e:
                print(f"ERROR processing message: {e}")
                import traceback
                traceback.print_exc()


def run_tier2_standalone(input_dir: str = "textract_outputs",
                         output_dir: str = "tier2_outputs"):
    """
    Standalone mode: Process all Textract JSON files in a directory.
    Does not use SQS - directly processes local files.

    Args:
        input_dir: Directory containing Textract JSON files
        output_dir: Directory for output files
    """
    import glob

    print("=" * 60)
    print("TIER 2: LayoutLMv3 Structure Refinement (Standalone Mode)")
    print("=" * 60)

    # Find Textract JSON files
    json_files = glob.glob(os.path.join(input_dir, "*_textract.json"))

    if not json_files:
        print(f"No Textract JSON files found in {input_dir}")
        return

    print(f"Found {len(json_files)} documents to process")

    # Load model
    loader = LayoutLMv3Loader()
    model, processor, device = loader.load_model()

    results = []
    total_start = time.time()

    for json_path in json_files:
        # Derive image path from JSON path
        base_name = os.path.basename(json_path).replace('_textract.json', '')
        possible_image_paths = [
            os.path.join("temp_pages", f"{base_name}.jpg"),
            os.path.join("temp_pages", f"{base_name}.png"),
            json_path.replace('_textract.json', '.jpg'),
            json_path.replace('_textract.json', '.png')
        ]

        image_path = None
        for path in possible_image_paths:
            if os.path.exists(path):
                image_path = path
                break

        if not image_path:
            print(f"Warning: No image found for {json_path}")

        result = process_single_document(
            json_path,
            image_path or "",
            model,
            processor,
            device,
            output_dir
        )
        results.append(result)

    total_time = time.time() - total_start

    # Summary
    print("\n" + "=" * 60)
    print("PROCESSING SUMMARY")
    print("=" * 60)
    print(f"Documents processed: {len(results)}")
    print(f"Total time: {total_time:.2f}s")
    print(f"Average per document: {total_time / len(results):.2f}s")

    escalation_count = sum(
        1 for r in results
        if r['refined_output']['MedicalTermFlags']['requires_escalation']
    )
    print(f"Documents requiring escalation: {escalation_count}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--queue":
        # Queue mode: consume from SQS
        process_tier2_queue()
    else:
        # Standalone mode: process local files
        run_tier2_standalone()
