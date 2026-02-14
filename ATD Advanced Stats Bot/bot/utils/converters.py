import pandas as pd

def safe_float_conversion(value):
    """Safely convert value to float"""
    if pd.isna(value) or value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None

def safe_str_conversion(value, default="N/A"):
    """Safely convert value to string"""
    if pd.isna(value) or value is None:
        return default
    return str(value)

def format_stat_value(value, stat_type, decimals):
    """Format stat value based on type"""
    if value is None or pd.isna(value):
        return "N/A"
    
    # Convert to float if possible
    try:
        float_val = float(value)
    except (ValueError, TypeError):
        return str(value)
    
    # Format based on type
    if stat_type == 'percentage':
        return f"{float_val:.{decimals}f}%"
    elif stat_type == 'float':
        if decimals == 3:
            return f"{float_val:.3f}"
        else:
            return f"{float_val:.{decimals}f}"
    else:
        return str(value)