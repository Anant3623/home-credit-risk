"""Meaningful boundaries for schema inference: unseen categories and exact amounts."""
from src.generate_ddl import infer_type,quote_ident

def test_status_cannot_be_inferred_integer_from_zero_sample():
    assert infer_type('STATUS',['0','0','1']) == 'TEXT'

def test_amount_has_no_rounding_scale_and_no_float():
    assert infer_type('AMT_PAYMENT',['123.450000001','0.0000001']) == 'NUMERIC'

def test_all_missing_numeric_field_remains_compatible_with_views():
    assert infer_type('AMT_ANNUITY',['','']) == 'NUMERIC'

def test_identifier_quotes_csv_header_safely():
    assert quote_ident('a"b') == '"a""b"'
