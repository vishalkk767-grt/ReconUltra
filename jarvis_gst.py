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
            'Invoice No (2B)': gstr_row['invoice_no_raw'],
            'Taxable (Books)': pr_row['taxable'],
            'Taxable (2B)': gstr_row['taxable'],
            'Tax Diff': round(pr_row['total_tax'] - gstr_row['total_tax'], 2),
            'IGST (Books)': pr_row['igst'], 'IGST (2B)': gstr_row['igst'],
            'CGST (Books)': pr_row['cgst'], 'CGST (2B)': gstr_row['cgst'],
            'SGST (Books)': pr_row['sgst'], 'SGST (2B)': gstr_row['sgst'],
            'Fuzzy Score': 100
        })
    
    # 2. Fuzzy Matches
    for pr_idx, gstr_idx, score, m_type in fuzzy_matches:
        pr_row = pr.loc[pr_idx]
        gstr_row = gstr.loc[gstr_idx]
        
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
            'Match Type': m_type,
            'GSTIN (Books)': pr_row['gstin'],
            'GSTIN (2B)': gstr_row['gstin'],
            'Party Name': pr_row['party_name'] if pr_row['party_name'] else gstr_row['party_name'],
            'Invoice No (Books)': pr_row['invoice_no_raw'],
            'Invoice No (2B)': gstr_row['invoice_no_raw'],
            'Taxable (Books)': pr_row['taxable'],
            'Taxable (2B)': gstr_row['taxable'],
            'Tax Diff': round(pr_row['total_tax'] - gstr_row['total_tax'], 2),
            'IGST (Books)': pr_row['igst'], 'IGST (2B)': gstr_row['igst'],
            'CGST (Books)': pr_row['cgst'], 'CGST (2B)': gstr_row['cgst'],
            'SGST (Books)': pr_row['sgst'], 'SGST (2B)': gstr_row['sgst'],
            'Fuzzy Score': score
        })

    # 3. Near Matches
    for pr_idx, gstr_idx in near_matches:
        pr_row = pr.loc[pr_idx]
        gstr_row = gstr.loc[gstr_idx]
        matched_rows.append({
            'Invoice Date (Books)': pr_row['invoice_date'],
            'Invoice Date (2B)': gstr_row['invoice_date'],
            'Status': 'Invoice Correction Needed',
            'Match Type': 'Near Match',
            'GSTIN (Books)': pr_row['gstin'],
            'GSTIN (2B)': gstr_row['gstin'],
            'Party Name': pr_row['party_name'] if pr_row['party_name'] else gstr_row['party_name'],
            'Invoice No (Books)': pr_row['invoice_no_raw'],
            'Invoice No (2B)': gstr_row['invoice_no_raw'],
            'Taxable (Books)': pr_row['taxable'],
            'Taxable (2B)': gstr_row['taxable'],
            'Tax Diff': round(pr_row['total_tax'] - gstr_row['total_tax'], 2),
            'IGST (Books)': pr_row['igst'], 'IGST (2B)': gstr_row['igst'],
            'CGST (Books)': pr_row['cgst'], 'CGST (2B)': gstr_row['cgst'],
            'SGST (Books)': pr_row['sgst'], 'SGST (2B)': gstr_row['sgst'],
            'Fuzzy Score': 95
        })

    # 4. Amount Fallback Matches
    for pr_idx, gstr_idx in amount_fallback_matches:
        pr_row = pr.loc[pr_idx]
        gstr_row = gstr.loc[gstr_idx]
        matched_rows.append({
            'Invoice Date (Books)': pr_row['invoice_date'],
            'Invoice Date (2B)': gstr_row['invoice_date'],
            'Status': 'Invoice Correction Needed',
            'Match Type': 'Amount Fallback',
            'GSTIN (Books)': pr_row['gstin'],
            'GSTIN (2B)': gstr_row['gstin'],
            'Party Name': pr_row['party_name'] if pr_row['party_name'] else gstr_row['party_name'],
            'Invoice No (Books)': pr_row['invoice_no_raw'],
            'Invoice No (2B)': gstr_row['invoice_no_raw'],
            'Taxable (Books)': pr_row['taxable'],
            'Taxable (2B)': gstr_row['taxable'],
            'Tax Diff': round(pr_row['total_tax'] - gstr_row['total_tax'], 2),
            'IGST (Books)': pr_row['igst'], 'IGST (2B)': gstr_row['igst'],
            'CGST (Books)': pr_row['cgst'], 'CGST (2B)': gstr_row['cgst'],
            'SGST (Books)': pr_row['sgst'], 'SGST (2B)': gstr_row['sgst'],
            'Fuzzy Score': 90
        })

    # 5. GSTIN Mismatch Matches
    for pr_idx, gstr_idx in gstin_mismatch_matches:
        pr_row = pr.loc[pr_idx]
        gstr_row = gstr.loc[gstr_idx]
        matched_rows.append({
            'Invoice Date (Books)': pr_row['invoice_date'],
            'Invoice Date (2B)': gstr_row['invoice_date'],
            'Status': 'GSTIN Mismatch',
            'Match Type': 'GSTIN Mismatch',
            'GSTIN (Books)': pr_row['gstin'],
            'GSTIN (2B)': gstr_row['gstin'],
            'Party Name': pr_row['party_name'] if pr_row['party_name'] else gstr_row['party_name'],
            'Invoice No (Books)': pr_row['invoice_no_raw'],
            'Invoice No (2B)': gstr_row['invoice_no_raw'],
            'Taxable (Books)': pr_row['taxable'],
            'Taxable (2B)': gstr_row['taxable'],
            'Tax Diff': round(pr_row['total_tax'] - gstr_row['total_tax'], 2),
            'IGST (Books)': pr_row['igst'], 'IGST (2B)': gstr_row['igst'],
            'CGST (Books)': pr_row['cgst'], 'CGST (2B)': gstr_row['cgst'],
            'SGST (Books)': pr_row['sgst'], 'SGST (2B)': gstr_row['sgst'],
            'Fuzzy Score': 90
        })
    
    matched_df = pd.DataFrame(matched_rows)
    
    matched_pr_orig = set()
    for key in exact_matched_keys:
        matched_pr_orig.add(pr[pr['key'] == key].index[0])
    for pr_idx, _, _, _ in fuzzy_matches:
        matched_pr_orig.add(pr_idx)
    for pr_idx, _ in near_matches:
        matched_pr_orig.add(pr_idx)
    for pr_idx, _ in amount_fallback_matches:
        matched_pr_orig.add(pr_idx)
    for pr_idx, _ in gstin_mismatch_matches:
        matched_pr_orig.add(pr_idx)
    
    # Missing in 2B
    missing_in_2b = pr[~pr.index.isin(matched_pr_orig)].copy()
    missing_in_2b = missing_in_2b[['invoice_date', 'gstin', 'party_name', 'invoice_no_raw', 'taxable', 'igst', 'cgst', 'sgst', 'total_tax']]
    missing_in_2b.columns = ['Invoice Date', 'GSTIN', 'Party Name', 'Invoice No', 'Taxable', 'IGST', 'CGST', 'SGST', 'Total Tax']
    
    # Extra in 2B
    extra_in_2b = gstr[~gstr.index.isin(matched_gstr_idxs)].copy()
    extra_in_2b = extra_in_2b[['invoice_date', 'gstin', 'party_name', 'invoice_no_raw', 'taxable', 'igst', 'cgst', 'sgst', 'total_tax']]
    extra_in_2b.columns = ['Invoice Date', 'GSTIN', 'Party Name', 'Invoice No', 'Taxable', 'IGST', 'CGST', 'SGST', 'Total Tax']
    
    return matched_df, missing_in_2b, extra_in_2b

def enrich_mismatch(df):
    if df.empty:
        return df
    df = df.copy()
    df['Taxable Diff'] = (df['Taxable (Books)'] - df['Taxable (2B)']).round(2)
    df['Taxable Side'] = df['Taxable Diff'].apply(
        lambda x: 'Books Higher' if x > 0 else ('2B Higher' if x < 0 else 'Equal')
    )
    df['IGST Diff'] = (df['IGST (Books)'] - df['IGST (2B)']).round(2)
    df['CGST Diff'] = (df['CGST (Books)'] - df['CGST (2B)']).round(2)
    df['SGST Diff'] = (df['SGST (Books)'] - df['SGST (2B)']).round(2)
    
    def make_summary(row):
        parts = []
        if abs(row['Taxable Diff']) > 0.01:
            parts.append(f"Taxable: {abs(row['Taxable Diff']):.2f} ({row['Taxable Side']})")
        if abs(row['Tax Diff']) > 0.01:
            side = 'Books Higher' if row['Tax Diff'] > 0 else '2B Higher'
            parts.append(f"Tax: {abs(row['Tax Diff']):.2f} ({side})")
        return " | ".join(parts) if parts else "No significant difference"
    
    df['Difference Summary'] = df.apply(make_summary, axis=1)
    return df

def to_excel_download(matched, missing, extra):
    output = BytesIO()
    
    if not matched.empty:
        matched_sorted = matched.sort_values(by=['Status', 'Invoice Date (Books)']).copy()
        mismatch_sub = matched_sorted[matched_sorted['Status'] == 'Value Mismatch'].copy()
        mismatch_sub = enrich_mismatch(mismatch_sub)
        other_matched = matched_sorted[matched_sorted['Status'] != 'Value Mismatch'].copy()
        final_matched_sheet = pd.concat([other_matched, mismatch_sub], ignore_index=True)
    else:
        final_matched_sheet = pd.DataFrame()
    
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        workbook = writer.book
        
        header_fmt = workbook.add_format({
            'bold': True, 'bg_color': '#1E3A5F', 'font_color': 'white',
            'border': 1, 'align': 'center', 'valign': 'vcenter'
        })
        yellow_fmt = workbook.add_format({'bg_color': '#FFEB9C', 'border': 1}) # Yellow for Value Mismatch
        red_fmt = workbook.add_format({'bg_color': '#FFC7CE', 'border': 1})    # Light Red for Missing & GSTIN Mismatch
        normal_fmt = workbook.add_format({'border': 1})
        money_fmt = workbook.add_format({'num_format': '#,##0.00', 'border': 1})
        
        # Write Matched Invoices Sheet with conditional row coloring
        if not final_matched_sheet.empty:
            df_m = final_matched_sheet
            df_m.to_excel(writer, sheet_name='Matched Invoices', index=False)
            ws = writer.sheets['Matched Invoices']
            
            for col_num, value in enumerate(df_m.columns.values):
                ws.write(0, col_num, value, header_fmt)
            
            for row_num in range(1, len(df_m) + 1):
                row_status = df_m.iloc[row_num-1]['Status']
                if row_status == 'Value Mismatch':
                    row_fmt = yellow_fmt
                elif row_status == 'GSTIN Mismatch':
                    row_fmt = red_fmt
                else:
                    row_fmt = normal_fmt
                
                for col_num in range(len(df_m.columns)):
                    val = df_m.iloc[row_num-1, col_num]
                    if any(x in str(df_m.columns[col_num]).lower() for x in ['taxable', 'igst', 'cgst', 'sgst', 'diff', 'tax']):
                        try:
                            ws.write_number(row_num, col_num, float(val) if pd.notna(val) else 0, money_fmt)
                            continue
                        except:
                            pass
                    ws.write(row_num, col_num, val if pd.notna(val) else '', row_fmt)
            
            for i, col in enumerate(df_m.columns):
                max_len = max(len(str(col)), df_m[col].astype(str).str.len().max() if len(df_m) > 0 else 10)
                ws.set_column(i, i, min(max_len + 2, 35))

        def write_sheet(df, sheet_name, row_format=None):
            if df.empty:
                return
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            ws = writer.sheets[sheet_name]
            
            for col_num, value in enumerate(df.columns.values):
                ws.write(0, col_num, value, header_fmt)
            
            for row_num in range(1, len(df) + 1):
                for col_num in range(len(df.columns)):
                    val = df.iloc[row_num-1, col_num]
                    fmt = row_format if row_format else normal_fmt
                    if any(x in str(df.columns[col_num]).lower() for x in ['taxable', 'igst', 'cgst', 'sgst', 'diff', 'tax']):
                        try:
                            ws.write_number(row_num, col_num, float(val) if pd.notna(val) else 0, money_fmt)
                            continue
                        except:
                            pass
                    ws.write(row_num, col_num, val if pd.notna(val) else '', fmt)
            
            for i, col in enumerate(df.columns):
                max_len = max(len(str(col)), df[col].astype(str).str.len().max() if len(df) > 0 else 10)
                ws.set_column(i, i, min(max_len + 2, 35))
        
        write_sheet(missing, 'Missing in 2B', red_fmt)
        write_sheet(extra, 'Extra in 2B', None)
        
        exact_cnt = len(matched[matched['Status'] == 'Exact Match']) if not matched.empty else 0
        date_mis_cnt = len(matched[matched['Status'] == 'Date Mismatch']) if not matched.empty else 0
        corr_cnt = len(matched[matched['Status'] == 'Invoice Correction Needed']) if not matched.empty else 0
        val_mis_cnt = len(matched[matched['Status'] == 'Value Mismatch']) if not matched.empty else 0
        gstin_mis_cnt = len(matched[matched['Status'] == 'GSTIN Mismatch']) if not matched.empty else 0
        
        summary_data = {
            'Category': [
                'Exact Match',
                'Date Mismatch',
                'Invoice Correction Needed',
                'Value Mismatch',
                'GSTIN Mismatch',
                'Missing in GSTR-2B',
                'Extra in GSTR-2B',
                'Total Invoices Processed (Books side)'
            ],
            'Count': [
                exact_cnt,
                date_mis_cnt,
                corr_cnt,
                val_mis_cnt,
                gstin_mis_cnt,
                len(missing),
                len(extra),
                exact_cnt + date_mis_cnt + corr_cnt + val_mis_cnt + gstin_mis_cnt + len(missing)
            ]
        }
        summary_df = pd.DataFrame(summary_data)
        summary_df.to_excel(writer, sheet_name='Summary', index=False)
        
        ws = writer.sheets['Summary']
        for col_num, value in enumerate(summary_df.columns.values):
            ws.write(0, col_num, value, header_fmt)
        ws.set_column(0, 0, 45)
        ws.set_column(1, 1, 15)
    
    output.seek(0)
    return output

def try_parse_gstr2b_json(file):
    try:
        data = json.load(file)
        rows = []
        docdata = None
        if 'data' in data:
            if 'docdata' in data['data']:
                docdata = data['data']['docdata']
            elif 'b2b' in data['data']:
                docdata = {'b2b': data['data']['b2b']}
        
        if not docdata:
            return None
        
        b2b = docdata.get('b2b', [])
        for supplier in b2b:
            gstin = supplier.get('ctin', '')
            trade_name = supplier.get('trdnm', '') or supplier.get('cname', '')
            invs = supplier.get('inv', [])
            for inv in invs:
                rows.append({
                    'GSTIN of Supplier': gstin,
                    'Party Name': trade_name,
                    'Invoice Number': inv.get('inum', ''),
                    'Invoice Date': inv.get('dt', ''),
                    'Taxable Value': inv.get('val', 0),
                    'IGST': inv.get('igst', 0) or inv.get('iamt', 0),
                    'CGST': inv.get('cgst', 0) or inv.get('camt', 0),
                    'SGST': inv.get('sgst', 0) or inv.get('samt', 0),
                })
        if rows:
            return pd.DataFrame(rows)
    except Exception as e:
        st.warning(f"JSON parse issue: {e}")
    return None

# --------------------------
# Sidebar
# --------------------------
with st.sidebar:
    st.markdown("## 🤖 Jarvis GST Ultimate")
    st.caption("Local Reconciliation Platform")
    st.markdown("---")
    
    st.markdown("### Settings")
    amount_tol = st.number_input("Amount Tolerance (₹)", min_value=0.0, value=2.0, step=0.5,
                                 help="Tax/Taxable difference within this is considered matched")
    fuzzy_th = st.slider("Fuzzy Match Threshold", 70, 100, 88,
                         help="Higher = stricter invoice number matching")
    max_date_gap = st.number_input("Max Date Gap for Fallback Match (Days)", min_value=0, max_value=30, value=2, step=1,
                                   help="If Invoice Numbers differ, match only if date gap is within these days")
    
    st.markdown("---")
    st.info("🔒 100% Local • Your data never leaves your computer")

# --------------------------
# Main UI
# --------------------------
st.markdown('<div class="main-header">🤖 Jarvis GST Ultimate</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Purchase Register ↔ GSTR-2B Reconciliation Platform</div>', unsafe_allow_html=True)

tab1, tab2, tab3 = st.tabs(["🔄 Reconcile", "📖 Guide", "ℹ️ About"])

with tab1:
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("1️⃣ Purchase Register (Books)")
        pr_file = st.file_uploader("Upload Excel / CSV", type=['xlsx', 'xls', 'csv'], key="pr")
    
    with col2:
        st.subheader("2️⃣ GSTR-2B")
        gstr_file = st.file_uploader("Upload Excel / CSV / JSON", type=['xlsx', 'xls', 'csv', 'json'], key="gstr")
    
    if pr_file and gstr_file:
        try:
            if pr_file.name.endswith('.csv'):
                pr_raw = pd.read_csv(pr_file)
            else:
                pr_raw = pd.read_excel(pr_file)
        except Exception as e:
            st.error(f"Purchase file error: {e}")
            st.stop()
        
        gstr_raw = None
        if gstr_file.name.endswith('.json'):
            gstr_raw = try_parse_gstr2b_json(gstr_file)
            if gstr_raw is None:
                st.error("Could not parse GSTR-2B JSON. Try Excel format instead.")
                st.stop()
        else:
            try:
                if gstr_file.name.endswith('.csv'):
                    gstr_raw = pd.read_csv(gstr_file)
                else:
                    gstr_raw = pd.read_excel(gstr_file)
            except Exception as e:
                st.error(f"GSTR-2B file error: {e}")
                st.stop()
        
        st.success(f"Loaded → Purchase: {len(pr_raw)} rows | GSTR-2B: {len(gstr_raw)} rows")
        
        st.markdown("### 3️⃣ Column Mapping")
        
        def auto_select(cols, keywords):
            cols_lower = [c.lower() for c in cols]
            for kw in keywords:
                for i, c in enumerate(cols_lower):
                    if kw in c:
                        return cols[i]
            return cols[0] if cols else None
        
        pr_cols = list(pr_raw.columns)
        gstr_cols = list(gstr_raw.columns)
        
        map_col1, map_col2 = st.columns(2)
        
        with map_col1:
            st.markdown("**Purchase Register columns**")
            pr_gstin = st.selectbox("Supplier GSTIN", pr_cols, index=pr_cols.index(auto_select(pr_cols, ['gstin', 'supplier gstin', 'ctin'])) if auto_select(pr_cols, ['gstin', 'supplier gstin', 'ctin']) in pr_cols else 0)
            pr_party = st.selectbox("Party / Supplier Name", ['-- None --'] + pr_cols, index=(['-- None --'] + pr_cols).index(auto_select(pr_cols, ['party', 'name', 'supplier', 'trade', 'customer'])) if auto_select(pr_cols, ['party', 'name', 'supplier', 'trade', 'customer']) else 0)
            pr_inv = st.selectbox("Invoice Number", pr_cols, index=pr_cols.index(auto_select(pr_cols, ['invoice', 'inv no', 'bill no'])) if auto_select(pr_cols, ['invoice', 'inv no', 'bill no']) in pr_cols else 0)
            pr_date = st.selectbox("Invoice Date", pr_cols, index=pr_cols.index(auto_select(pr_cols, ['date', 'dt', 'inv date'])) if auto_select(pr_cols, ['date', 'dt', 'inv date']) in pr_cols else 0)
            pr_taxable = st.selectbox("Taxable Value", pr_cols, index=pr_cols.index(auto_select(pr_cols, ['taxable', 'taxable value'])) if auto_select(pr_cols, ['taxable', 'taxable value']) in pr_cols else 0)
            pr_igst = st.selectbox("IGST", ['-- None --'] + pr_cols, index=(['-- None --'] + pr_cols).index(auto_select(pr_cols, ['igst', 'integrated'])) if auto_select(pr_cols, ['igst', 'integrated']) else 0)
            pr_cgst = st.selectbox("CGST", ['-- None --'] + pr_cols, index=(['-- None --'] + pr_cols).index(auto_select(pr_cols, ['cgst', 'central'])) if auto_select(pr_cols, ['cgst', 'central']) else 0)
            pr_sgst = st.selectbox("SGST / UTGST", ['-- None --'] + pr_cols, index=(['-- None --'] + pr_cols).index(auto_select(pr_cols, ['sgst', 'utgst'])) if auto_select(pr_cols, ['sgst', 'utgst']) else 0)
        
        with map_col2:
            st.markdown("**GSTR-2B columns**")
            g_gstin = st.selectbox("Supplier GSTIN", gstr_cols, key="g_gstin", index=gstr_cols.index(auto_select(gstr_cols, ['gstin', 'supplier gstin', 'ctin'])) if auto_select(gstr_cols, ['gstin', 'supplier gstin', 'ctin']) in gstr_cols else 0)
            g_party = st.selectbox("Party / Supplier Name", ['-- None --'] + gstr_cols, key="g_party", index=(['-- None --'] + gstr_cols).index(auto_select(gstr_cols, ['party', 'name', 'supplier', 'trade', 'trdnm', 'cname'])) if auto_select(gstr_cols, ['party', 'name', 'supplier', 'trade', 'trdnm', 'cname']) else 0)
            g_inv = st.selectbox("Invoice Number", gstr_cols, key="g_inv", index=gstr_cols.index(auto_select(gstr_cols, ['invoice', 'inv no', 'inum'])) if auto_select(gstr_cols, ['invoice', 'inv no', 'inum']) in gstr_cols else 0)
            g_date = st.selectbox("Invoice Date", gstr_cols, key="g_date", index=gstr_cols.index(auto_select(gstr_cols, ['date', 'dt', 'inv date'])) if auto_select(gstr_cols, ['date', 'dt', 'inv date']) in gstr_cols else 0)
            g_taxable = st.selectbox("Taxable Value", gstr_cols, key="g_taxable", index=gstr_cols.index(auto_select(gstr_cols, ['taxable', 'val'])) if auto_select(gstr_cols, ['taxable', 'val']) in gstr_cols else 0)
            g_igst = st.selectbox("IGST", ['-- None --'] + gstr_cols, key="g_igst", index=(['-- None --'] + gstr_cols).index(auto_select(gstr_cols, ['igst', 'iamt'])) if auto_select(gstr_cols, ['igst', 'iamt']) else 0)
            g_cgst = st.selectbox("CGST", ['-- None --'] + gstr_cols, key="g_cgst", index=(['-- None --'] + gstr_cols).index(auto_select(gstr_cols, ['cgst', 'camt'])) if auto_select(gstr_cols, ['cgst', 'camt']) else 0)
            g_sgst = st.selectbox("SGST / UTGST", ['-- None --'] + gstr_cols, key="g_sgst", index=(['-- None --'] + gstr_cols).index(auto_select(gstr_cols, ['sgst', 'samt'])) if auto_select(gstr_cols, ['sgst', 'samt']) else 0)
        
        if st.button("🚀 Run Advanced Reconciliation", type="primary", use_container_width=True):
            with st.spinner("Jarvis is running ultimate reconciliation..."):
                pr_map = {
                    'gstin': pr_gstin,
                    'party_name': pr_party if pr_party != '-- None --' else None,
                    'invoice_no': pr_inv,
                    'invoice_date': pr_date if pr_date != '-- None --' else None,
                    'taxable': pr_taxable,
                    'igst': pr_igst if pr_igst != '-- None --' else None,
                    'cgst': pr_cgst if pr_cgst != '-- None --' else None,
                    'sgst': pr_sgst if pr_sgst != '-- None --' else None,
                }
                g_map = {
                    'gstin': g_gstin,
                    'party_name': g_party if g_party != '-- None --' else None,
                    'invoice_no': g_inv,
                    'invoice_date': g_date if g_date != '-- None --' else None,
                    'taxable': g_taxable,
                    'igst': g_igst if g_igst != '-- None --' else None,
                    'cgst': g_cgst if g_cgst != '-- None --' else None,
                    'sgst': g_sgst if g_sgst != '-- None --' else None,
                }
                
                pr_norm = normalize_df(pr_raw, pr_map)
                gstr_norm = normalize_df(gstr_raw, g_map)
                
                pr_norm = pr_norm[(pr_norm['gstin'] != '') & (pr_norm['invoice_no'] != '')]
                gstr_norm = gstr_norm[(gstr_norm['gstin'] != '') & (gstr_norm['invoice_no'] != '')]
                
                matched, missing, extra = reconcile(
                    pr_norm, gstr_norm, 
                    amount_tolerance=amount_tol,
                    fuzzy_threshold=fuzzy_th,
                    max_date_gap_days=max_date_gap
                )
                
                st.session_state['matched'] = matched
                st.session_state['missing'] = missing
                st.session_state['extra'] = extra
                st.session_state['done'] = True
        
        if st.session_state.get('done'):
            matched = st.session_state['matched']
            missing = st.session_state['missing']
            extra = st.session_state['extra']
            
            st.markdown("---")
            st.subheader("📊 Reconciliation Summary Dashboard")
            
            exact_cnt = len(matched[matched['Status'] == 'Exact Match']) if not matched.empty else 0
            date_mis_cnt = len(matched[matched['Status'] == 'Date Mismatch']) if not matched.empty else 0
            corr_cnt = len(matched[matched['Status'] == 'Invoice Correction Needed']) if not matched.empty else 0
            val_mis_cnt = len(matched[matched['Status'] == 'Value Mismatch']) if not matched.empty else 0
            gstin_mis_cnt = len(matched[matched['Status'] == 'GSTIN Mismatch']) if not matched.empty else 0
            
            m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
            m1.metric("✅ Exact", exact_cnt)
            m2.metric("📅 Date", date_mis_cnt)
            m3.metric("🟤 Correction", corr_cnt)
            m4.metric("⚠️ Value", val_mis_cnt)
            m5.metric("🔄 GSTIN Mismatch", gstin_mis_cnt)
            m6.metric("🔴 Missing", len(missing))
            m7.metric("🟡 Extra", len(extra))
            
            excel_bytes = to_excel_download(matched, missing, extra)
            st.download_button(
                label="📥 Download Ultimate Excel Report",
                data=excel_bytes,
                file_name=f"Jarvis_GST_Reconciliation_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
            
            r1, r2, r3 = st.tabs(["🔗 Matched Invoices", "🔴 Missing in 2B", "🟡 Extra in 2B"])
            
            with r1:
                if not matched.empty:
                    st.dataframe(matched, use_container_width=True, height=400)
                else:
                    st.info("No matched invoices found.")

            with r2:
                if not missing.empty:
                    st.warning("These invoices are in your books but missing in GSTR-2B.")
                    st.dataframe(missing, use_container_width=True, height=400)
                else:
                    st.success("No missing invoices.")
            
            with r3:
                if not extra.empty:
                    st.info("These appear in GSTR-2B but are not found in your books.")
                    st.dataframe(extra, use_container_width=True, height=400)
                else:
                    st.success("No extra invoices in 2B.")

with tab2:
    st.markdown("""
    ### Ultimate Reconciliation Guide
    1. **Matched Invoices Sheet**: Exact Match, Date Mismatch, Invoice Correction, Value Mismatch aur GSTIN Mismatch sab ek hi sheet par hain. Value Mismatch rows yellow highlight hongi, aur GSTIN Mismatch rows halka red highlight hongi.
    2. **Missing in 2B Sheet**: Halka red highlight hoga aur party name ke sath sabse pehle date hogi.
    3. **Date First Column**: Sabhi sheets mein Date sabse pehla column hai.
    4. **Strict Date Gap Feature**: Agar invoice numbers alag honge, toh fallback/near matching tabhi hogi jab date ka gap sidebar ke set 'Max Date Gap' (Default: 2 days) ke andar hoga.
    """)

with tab3:
    st.markdown("### About Jarvis GST Ultimate")

st.markdown("---")
st.caption("Jarvis GST Ultimate • Local Reconciliation Platform")
