"""
Jarvis GST Reconciliation Platform (Ultimate Professional Edition)
--------------------------------------------------
Full local ownership. Purchase Register vs GSTR-2B matching for B2B invoices.
Features: Single Matched Sheet, Party Name Included, Date as 1st Column, GSTIN Mismatch Detection, Smart Fallback with Strict Date Gap.
"""

import streamlit as st
import pandas as pd
import numpy as np
from rapidfuzz import fuzz, process
import re
from io import BytesIO
from datetime import datetime
import json

# --------------------------
# Page Config
# --------------------------
st.set_page_config(
    page_title="Jarvis GST | Advanced Reconciliation Platform",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
<style>
    .main-header {
        font-size: 2.4rem;
        font-weight: 700;
        color: #1E3A5F;
        margin-bottom: 0.2rem;
    }
    .sub-header {
        font-size: 1.1rem;
        color: #5A6A7A;
        margin-bottom: 1.5rem;
    }
    .stDownloadButton button {
        background-color: #2563EB;
        color: white;
        border-radius: 8px;
    }
</style>
""", unsafe_allow_html=True)

# --------------------------
# Helper Functions & Cleaning
# --------------------------

def clean_gstin(val):
    if pd.isna(val):
        return ""
    return str(val).strip().upper().replace(" ", "").replace("\xa0", "")

def clean_invoice(val):
    if pd.isna(val):
        return ""
    s = str(val).strip().upper().replace("\xa0", "")
    if s.endswith('.0'):
        s = s[:-2]
    return re.sub(r'[\s\.\-_/\\]', '', s)

def get_normalized_inv_for_matching(val):
    if pd.isna(val):
        return ""
    s = str(val).strip().upper().replace("\xa0", "")
    if s.endswith('.0'):
        s = s[:-2]
    for prefix in ['INV', 'BILL', 'GST', 'NO', 'B']:
        if s.startswith(prefix) and len(s) > len(prefix):
            s = s[len(prefix):]
    s = re.sub(r'[\s\.\-_/\\]', '', s)
    s = s.lstrip('0')
    return s if s else '0'

def clean_amount(val):
    try:
        if pd.isna(val):
            return 0.0
        if isinstance(val, str):
            val = val.replace(',', '').replace('₹', '').strip()
        return round(float(val), 2)
    except:
        return 0.0

def parse_date(val):
    if pd.isna(val) or val == "" or str(val).strip() == "":
        return None
    try:
        dt = pd.to_datetime(val, dayfirst=True, errors='coerce')
        if pd.notna(dt):
            return dt
    except:
        pass
    return None

def format_date_str(dt):
    if pd.notna(dt) and dt is not None:
        return dt.strftime('%d-%m-%Y')
    return ""

def calculate_date_diff_days(d1, d2):
    if pd.notna(d1) and pd.notna(d2) and d1 is not None and d2 is not None:
        return abs((d1 - d2).days)
    return 9999

def normalize_df(df, mapping):
    result = pd.DataFrame()
    result['gstin'] = df[mapping['gstin']].apply(clean_gstin)
    result['party_name'] = df[mapping['party_name']].astype(str).str.strip() if mapping.get('party_name') and mapping['party_name'] in df.columns else ""
    result['invoice_no'] = df[mapping['invoice_no']].apply(clean_invoice)
    result['invoice_no_match'] = df[mapping['invoice_no']].apply(get_normalized_inv_for_matching)
    result['invoice_no_raw'] = df[mapping['invoice_no']].astype(str).str.strip()
    result['parsed_date'] = df[mapping['invoice_date']].apply(parse_date) if mapping.get('invoice_date') else None
    result['invoice_date'] = result['parsed_date'].apply(format_date_str)
    result['taxable'] = df[mapping['taxable']].apply(clean_amount) if mapping.get('taxable') else 0.0
    
    result['igst'] = df[mapping['igst']].apply(clean_amount) if (mapping.get('igst') and mapping['igst'] in df.columns) else 0.0
    result['cgst'] = df[mapping['cgst']].apply(clean_amount) if (mapping.get('cgst') and mapping['cgst'] in df.columns) else 0.0
    result['sgst'] = df[mapping['sgst']].apply(clean_amount) if (mapping.get('sgst') and mapping['sgst'] in df.columns) else 0.0
    
    result['total_tax'] = result['igst'] + result['cgst'] + result['sgst']
    result['orig_idx'] = df.index
    return result

def create_match_key(gstin, inv):
    return f"{gstin}|{inv}"

# --------------------------
# Reconciliation Logic
# --------------------------

def check_date_match(date1, date2):
    if not date1 or not date2:
        return True
    return str(date1).strip() == str(date2).strip()

def reconcile(pr_df, gstr_df, amount_tolerance=2.0, fuzzy_threshold=88, max_date_gap_days=2):
    pr = pr_df.copy()
    gstr = gstr_df.copy()
    
    # Level 1: Exact Match
    pr['key'] = pr.apply(lambda r: create_match_key(r['gstin'], r['invoice_no']), axis=1)
    gstr['key'] = gstr.apply(lambda r: create_match_key(r['gstin'], r['invoice_no']), axis=1)
    
    pr_keys = set(pr['key'])
    gstr_keys = set(gstr['key'])
    exact_matched_keys = pr_keys & gstr_keys
    
    remaining_pr = pr[~pr['key'].isin(exact_matched_keys)].copy()
    remaining_gstr = gstr[~gstr['key'].isin(exact_matched_keys)].copy()
    
    # Level 2: Fuzzy Match
    fuzzy_matches = []
    for gstin, pr_group in remaining_pr.groupby('gstin'):
        gstr_group = remaining_gstr[remaining_gstr['gstin'] == gstin]
        if gstr_group.empty:
            continue
        gstr_invs = gstr_group['invoice_no_match'].tolist()
        gstr_idx_map = dict(zip(gstr_group['invoice_no_match'], gstr_group.index))
        
        used_gstr = set()
        for pr_idx, row in pr_group.iterrows():
            match = process.extractOne(row['invoice_no_match'], [i for i in gstr_invs if i not in used_gstr], scorer=fuzz.ratio)
            if match and match[1] >= fuzzy_threshold:
                gstr_idx = gstr_idx_map[match[0]]
                fuzzy_matches.append((pr_idx, gstr_idx, match[1], 'Fuzzy / Normalized'))
                used_gstr.add(match[0])

    matched_pr_idxs = set()
    for key in exact_matched_keys:
        matched_pr_idxs.add(pr[pr['key'] == key].index[0])
    for pr_idx, _, _, _ in fuzzy_matches:
        matched_pr_idxs.add(pr_idx)
        
    matched_gstr_idxs = set()
    for key in exact_matched_keys:
        matched_gstr_idxs.add(gstr[gstr['key'] == key].index[0])
    for _, gstr_idx, _, _ in fuzzy_matches:
        matched_gstr_idxs.add(gstr_idx)

    remaining_pr = pr[~pr.index.isin(matched_pr_idxs)].copy()
    remaining_gstr = gstr[~gstr.index.isin(matched_gstr_idxs)].copy()

    # Level 3: Near Match (Substring match with strict date gap check)
    near_matches = []
    for pr_idx, pr_row in remaining_pr.iterrows():
        gstr_subset = remaining_gstr[remaining_gstr['gstin'] == pr_row['gstin']]
        if gstr_subset.empty:
            continue
        for gstr_idx, gstr_row in gstr_subset.iterrows():
            if gstr_idx in matched_gstr_idxs:
                continue
            p_inv = pr_row['invoice_no_match']
            g_inv = gstr_row['invoice_no_match']
            
            partial_cond = (p_inv in g_inv) or (g_inv in p_inv) or (pr_row['invoice_no'] in gstr_row['invoice_no'])
            taxable_diff = abs(pr_row['taxable'] - gstr_row['taxable'])
            tax_diff = abs(pr_row['total_tax'] - gstr_row['total_tax'])
            date_gap = calculate_date_diff_days(pr_row['parsed_date'], gstr_row['parsed_date'])
            
            # STRICT DATE CHECK (<= max_date_gap_days)
            if partial_cond and taxable_diff <= amount_tolerance and tax_diff <= amount_tolerance and date_gap <= max_date_gap_days:
                near_matches.append((pr_idx, gstr_idx))
                matched_gstr_idxs.add(gstr_idx)
                break

    for pr_idx, _ in near_matches:
        matched_pr_idxs.add(pr_idx)

    remaining_pr = pr[~pr.index.isin(matched_pr_idxs)].copy()
    remaining_gstr = gstr[~gstr.index.isin(matched_gstr_idxs)].copy()

    # Level 4: Smart Amount Fallback Match (Different Inv No, Same Amount, STRICT Date Gap <= 2 days)
    amount_fallback_matches = []
    for pr_idx, pr_row in remaining_pr.iterrows():
        gstr_subset = remaining_gstr[remaining_gstr['gstin'] == pr_row['gstin']]
        if gstr_subset.empty:
            continue
        for gstr_idx, gstr_row in gstr_subset.iterrows():
            if gstr_idx in matched_gstr_idxs:
                continue
            taxable_diff = abs(pr_row['taxable'] - gstr_row['taxable'])
            tax_diff = abs(pr_row['total_tax'] - gstr_row['total_tax'])
            date_gap = calculate_date_diff_days(pr_row['parsed_date'], gstr_row['parsed_date'])
            
            # STRICT CHECK: Amount match ho raha hai lekin date ka gap 1 या 2 din se zyada (jaise mahino ka) hai to NO MATCH
            if taxable_diff <= amount_tolerance and tax_diff <= amount_tolerance and date_gap <= max_date_gap_days:
                amount_fallback_matches.append((pr_idx, gstr_idx))
                matched_gstr_idxs.add(gstr_idx)
                break

    for pr_idx, _ in amount_fallback_matches:
        matched_pr_idxs.add(pr_idx)

    remaining_pr = pr[~pr.index.isin(matched_pr_idxs)].copy()
    remaining_gstr = gstr[~gstr.index.isin(matched_gstr_idxs)].copy()

    # Level 5: GSTIN Mismatch Check (Same Invoice No + Same Amount + Strict Date Gap, but Different GSTIN)
    gstin_mismatch_matches = []
    for pr_idx, pr_row in remaining_pr.iterrows():
        match_subset = remaining_gstr[
            (remaining_gstr['invoice_no_match'] == pr_row['invoice_no_match']) &
            (abs(remaining_gstr['taxable'] - pr_row['taxable']) <= amount_tolerance) &
            (abs(remaining_gstr['total_tax'] - pr_row['total_tax']) <= amount_tolerance)
        ]
        for gstr_idx, gstr_row in match_subset.iterrows():
            if gstr_idx not in matched_gstr_idxs:
                date_gap = calculate_date_diff_days(pr_row['parsed_date'], gstr_row['parsed_date'])
                if date_gap <= max_date_gap_days:
                    gstin_mismatch_matches.append((pr_idx, gstr_idx))
                    matched_gstr_idxs.add(gstr_idx)
                    break

    matched_rows = []
    
    # 1. Exact Matches
    for key in exact_matched_keys:
        pr_row = pr[pr['key'] == key].iloc[0]
        gstr_row = gstr[gstr['key'] == key].iloc[0]
        
        tax_diff = abs(pr_row['total_tax'] - gstr_row['total_tax'])
        taxable_diff = abs(pr_row['taxable'] - gstr_row['taxable'])
        date_ok = check_date_match(pr_row['invoice_date'], gstr_row['invoice_date'])
        
        if tax_diff <= amount_tolerance and taxable_diff <= amount_tolerance:
            status = "Exact Match" if date_ok else "Date Mismatch"
        else:
            status = "Value Mismatch"
        
        matched_rows.append({
            'Invoice Date (Books)': pr_row['invoice_date'],
            'Invoice Date (2B)': gstr_row['invoice_date'],
            'Status': status,
            'Match Type': 'Exact',
            'GSTIN (Books)': pr_row['gstin'],
            'GSTIN (2B)': gstr_row['gstin'],
            'Party Name': pr_row['party_name'] if pr_row['party_name'] else gstr_row['party_name'],
            'Invoice No (Books)': pr_row['invoice_no_raw'],
            'Invoice No (2B)': g
