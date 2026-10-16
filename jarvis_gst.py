"""
Jarvis GST Reconciliation Platform (3-Level Matching Edition)
--------------------------------------------------
Full local ownership. Purchase Register vs GSTR-2B matching for B2B invoices.
Features: 3-Level Matching (Exact, Fuzzy, Near/Partial Match), Value Mismatch, Missing/Extra.
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
    page_title="Jarvis GST | Reconciliation Platform",
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
    return str(val).strip().upper().replace(" ", "")

def clean_invoice(val):
    if pd.isna(val):
        return ""
    s = str(val).strip().upper()
    return re.sub(r'[\s\.\-_/\\]', '', s)

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
    if pd.isna(val):
        return None
    try:
        return pd.to_datetime(val, dayfirst=True, errors='coerce')
    except:
        return None

def normalize_df(df, mapping):
    result = pd.DataFrame()
    result['gstin'] = df[mapping['gstin']].apply(clean_gstin)
    result['invoice_no'] = df[mapping['invoice_no']].apply(clean_invoice)
    result['invoice_no_raw'] = df[mapping['invoice_no']].astype(str).str.strip()
    result['invoice_date'] = df[mapping['invoice_date']].apply(parse_date) if mapping.get('invoice_date') else None
    result['taxable'] = df[mapping['taxable']].apply(clean_amount) if mapping.get('taxable') else 0.0
    
    igst = df[mapping['igst']].apply(clean_amount) if mapping.get('igst') else 0.0
    cgst = df[mapping['cgst']].apply(clean_amount) if mapping.get('cgst') else 0.0
    sgst = df[mapping['sgst']].apply(clean_amount) if mapping.get('sgst') else 0.0
    
    result['igst'] = igst
    result['cgst'] = cgst
    result['sgst'] = sgst
    result['total_tax'] = igst + cgst + sgst
    result['orig_idx'] = df.index
    return result

def create_match_key(gstin, inv):
    return f"{gstin}|{inv}"

# --------------------------
# 3-Level Reconciliation Logic
# --------------------------

def reconcile(pr_df, gstr_df, amount_tolerance=2.0, fuzzy_threshold=88):
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
        gstr_invs = gstr_group['invoice_no'].tolist()
        gstr_idx_map = dict(zip(gstr_group['invoice_no'], gstr_group.index))
        
        used_gstr = set()
        for pr_idx, row in pr_group.iterrows():
            match = process.extractOne(row['invoice_no'], [i for i in gstr_invs if i not in used_gstr], scorer=fuzz.ratio)
            if match and match[1] >= fuzzy_threshold:
                gstr_idx = gstr_idx_map[match[0]]
                fuzzy_matches.append((pr_idx, gstr_idx, match[1], 'Fuzzy'))
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

    # Level 3: Near / Partial Match (Invoice Correction)
    near_matches = []
    for pr_idx, pr_row in remaining_pr.iterrows():
        gstr_subset = remaining_gstr[remaining_gstr['gstin'] == pr_row['gstin']]
        if gstr_subset.empty:
            continue
        for gstr_idx, gstr_row in gstr_subset.iterrows():
            if gstr_idx in matched_gstr_idxs:
                continue
            p_inv = pr_row['invoice_no']
            g_inv = gstr_row['invoice_no']
            
            partial_cond = (p_inv in g_inv) or (g_inv in p_inv)
            taxable_diff = abs(pr_row['taxable'] - gstr_row['taxable'])
            tax_diff = abs(pr_row['total_tax'] - gstr_row['total_tax'])
            
            if partial_cond and taxable_diff <= amount_tolerance and tax_diff <= amount_tolerance:
                near_matches.append((pr_idx, gstr_idx))
                matched_gstr_idxs.add(gstr_idx)
                break

    matched_rows = []
    
    # 1. Exact Matches
    for key in exact_matched_keys:
        pr_row = pr[pr['key'] == key].iloc[0]
        gstr_row = gstr[gstr['key'] == key].iloc[0]
        
        tax_diff = abs(pr_row['total_tax'] - gstr_row['total_tax'])
        taxable_diff = abs(pr_row['taxable'] - gstr_row['taxable'])
        
        status = "Fully Matched" if (tax_diff <= amount_tolerance and taxable_diff <= amount_tolerance) else "Value Mismatch"
        
        matched_rows.append({
            'Status': status, 'Match Type': 'Exact', 'GSTIN': pr_row['gstin'],
            'Invoice No (Books)': pr_row['invoice_no_raw'], 'Invoice No (2B)': gstr_row['invoice_no_raw'],
            'Taxable (Books)': pr_row['taxable'], 'Taxable (2B)': gstr_row['taxable'],
            'Taxable Diff': round(pr_row['taxable'] - gstr_row['taxable'], 2),
            'Total Tax (Books)': pr_row['total_tax'], 'Total Tax (2B)': gstr_row['total_tax'],
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
        
        status = "Fully Matched" if (tax_diff <= amount_tolerance and taxable_diff <= amount_tolerance) else "Value Mismatch"
        
        matched_rows.append({
            'Status': status, 'Match Type': 'Fuzzy', 'GSTIN': pr_row['gstin'],
            'Invoice No (Books)': pr_row['invoice_no_raw'], 'Invoice No (2B)': gstr_row['invoice_no_raw'],
            'Taxable (Books)': pr_row['taxable'], 'Taxable (2B)': gstr_row['taxable'],
            'Taxable Diff': round(pr_row['taxable'] - gstr_row['taxable'], 2),
            'Total Tax (Books)': pr_row['total_tax'], 'Total Tax (2B)': gstr_row['total_tax'],
            'Tax Diff': round(pr_row['total_tax'] - gstr_row['total_tax'], 2),
            'IGST (Books)': pr_row['igst'], 'IGST (2B)': gstr_row['igst'],
            'CGST (Books)': pr_row['cgst'], 'CGST (2B)': gstr_row['cgst'],
            'SGST (Books)': pr_row['sgst'], 'SGST (2B)': gstr_row['sgst'],
            'Fuzzy Score': score
        })

    # 3. Near Matches (Invoice Correction Needed)
    for pr_idx, gstr_idx in near_matches:
        pr_row = pr.loc[pr_idx]
        gstr_row = gstr.loc[gstr_idx]
        
        matched_rows.append({
            'Status': 'Invoice Correction Needed', 'Match Type': 'Near Match', 'GSTIN': pr_row['gstin'],
            'Invoice No (Books)': pr_row['invoice_no_raw'], 'Invoice No (2B)': gstr_row['invoice_no_raw'],
            'Taxable (Books)': pr_row['taxable'], 'Taxable (2B)': gstr_row['taxable'],
            'Taxable Diff': round(pr_row['taxable'] - gstr_row['taxable'], 2),
            'Total Tax (Books)': pr_row['total_tax'], 'Total Tax (2B)': gstr_row['total_tax'],
            'Tax Diff': round(pr_row['total_tax'] - gstr_row['total_tax'], 2),
            'IGST (Books)': pr_row['igst'], 'IGST (2B)': gstr_row['igst'],
            'CGST (Books)': pr_row['cgst'], 'CGST (2B)': gstr_row['cgst'],
            'SGST (Books)': pr_row['sgst'], 'SGST (2B)': gstr_row['sgst'],
            'Fuzzy Score': 95
        })

    matched_df = pd.DataFrame(matched_rows)
    
    # Collect all matched original PR indexes
    all_matched_pr_idxs = set()
    for key in exact_matched_keys:
        all_matched_pr_idxs.add(pr[pr['key'] == key].index[0])
    for pr_idx, _, _, _ in fuzzy_matches:
        all_matched_pr_idxs.add(pr_idx)
    for pr_idx, _ in near_matches:
        all_matched_pr_idxs.add(pr_idx)

    missing_in_2b = pr[~pr.index.isin(all_matched_pr_idxs)].copy()
    missing_in_2b = missing_in_2b[['gstin', 'invoice_no_raw', 'taxable', 'igst', 'cgst', 'sgst', 'total_tax']]
    missing_in_2b.columns = ['GSTIN', 'Invoice No', 'Taxable', 'IGST', 'CGST', 'SGST', 'Total Tax']
    
    extra_in_2b = gstr[~gstr.index.isin(matched_gstr_idxs)].copy()
    extra_in_2b = extra_in_2b[['gstin', 'invoice_no_raw', 'taxable', 'igst', 'cgst', 'sgst', 'total_tax']]
    extra_in_2b.columns = ['GSTIN', 'Invoice No', 'Taxable', 'IGST', 'CGST', 'SGST', 'Total Tax']
    
    return matched_df, missing_in_2b, extra_in_2b

def to_excel_download(matched, missing, extra):
    output = BytesIO()
    
    if not matched.empty:
        exact_df = matched[matched['Match Type'] == 'Exact'].copy()
        fuzzy_df = matched[matched['Match Type'] == 'Fuzzy'].copy()
        near_df = matched[matched['Match Type'] == 'Near Match'].copy()
        mismatch_df = matched[matched['Status'] == 'Value Mismatch'].copy()
    else:
        exact_df = pd.DataFrame()
        fuzzy_df = pd.DataFrame()
        near_df = pd.DataFrame()
        mismatch_df = pd.DataFrame()
        
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        workbook = writer.book
        
        header_fmt = workbook.add_format({
            'bold': True, 'bg_color': '#1E3A5F', 'font_color': 'white',
            'border': 1, 'align': 'center', 'valign': 'vcenter'
        })
        
        normal_fmt = workbook.add_format({'border': 1})
        money_fmt = workbook.add_format({'num_format': '#,##0.00', 'border': 1})
        
        def write_sheet(df, sheet_name):
            if df.empty:
                return
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            ws = writer.sheets[sheet_name]
            
            for col_num, value in enumerate(df.columns.values):
                ws.write(0, col_num, value, header_fmt)
            
            for row_num in range(1, len(df) + 1):
                for col_num in range(len(df.columns)):
                    val = df.iloc[row_num-1, col_num]
                    if any(x in str(df.columns[col_num]).lower() for x in ['taxable', 'igst', 'cgst', 'sgst', 'diff', 'tax']):
                        try:
                            ws.write_number(row_num, col_num, float(val) if pd.notna(val) else 0, money_fmt)
                            continue
                        except:
                            pass
                    ws.write(row_num, col_num, val if pd.notna(val) else '', normal_fmt)
            
            for i, col in enumerate(df.columns):
                max_len = max(len(str(col)), df[col].astype(str).str.len().max() if len(df) > 0 else 10)
                ws.set_column(i, i, min(max_len + 2, 35))

        write_sheet(exact_df, 'Exact Match')
        write_sheet(fuzzy_df, 'Fuzzy Match')
        write_sheet(near_df, 'Invoice Correction')
        write_sheet(mismatch_df, 'Value Mismatch')
        write_sheet(missing, 'Missing in 2B')
        write_sheet(extra, 'Extra in 2B')
        
        summary_data = {
            'Category': [
                'Exact Match Invoices',
                'Fuzzy Match Invoices',
                'Invoice Correction (Near Match)',
                'Value Mismatch Invoices',
                'Missing in GSTR-2B',
                'Extra in GSTR-2B',
                'Total Invoices Processed (Books)'
            ],
            'Count': [
                len(exact_df),
                len(fuzzy_df),
                len(near_df),
                len(mismatch_df),
                len(missing),
                len(extra),
                len(exact_df) + len(fuzzy_df) + len(near_df) + len(mismatch_df) + len(missing)
            ]
        }
        summary_df = pd.DataFrame(summary_data)
        summary_df.to_excel(writer, sheet_name='Summary', index=False)
        
        ws = writer.sheets['Summary']
        for col_num, value in enumerate(summary_df.columns.values):
            ws.write(0, col_num, value, header_fmt)
        ws.set_column(0, 0, 40)
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
            invs = supplier.get('inv', [])
            for inv in invs:
                rows.append({
                    'GSTIN of Supplier': gstin,
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
    st.markdown("## 🤖 Jarvis GST")
    st.caption("Local Reconciliation Platform")
    st.markdown("---")
    
    st.markdown("### Settings")
    amount_tol = st.number_input("Amount Tolerance (₹)", min_value=0.0, value=2.0, step=0.5,
                                 help="Tax/Taxable difference within this is considered matched")
    fuzzy_th = st.slider("Fuzzy Match Threshold", 70, 100, 88,
                         help="Higher = stricter invoice number matching")
    
    st.markdown("---")
    st.info("🔒 100% Local • Your data never leaves your computer")

# --------------------------
# Main UI
# --------------------------
st.markdown('<div class="main-header">🤖 Jarvis GST</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Purchase Register ↔ GSTR-2B 3-Level Reconciliation Platform</div>', unsafe_allow_html=True)

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
        pr_inv = st.selectbox("Invoice Number", pr_cols, index=pr_cols.index(auto_select(pr_cols, ['invoice', 'inv no', 'bill no'])) if auto_select(pr_cols, ['invoice', 'inv no', 'bill no']) in pr_cols else 0)
        pr_date = st.selectbox("Invoice Date (optional)", ['-- None --'] + pr_cols, index=0)
        pr_taxable = st.selectbox("Taxable Value", pr_cols, index=pr_cols.index(auto_select(pr_cols, ['taxable', 'taxable value'])) if auto_select(pr_cols, ['taxable', 'taxable value']) in pr_cols else 0)
        pr_igst = st.selectbox("IGST", ['-- None --'] + pr_cols, index=(['-- None --'] + pr_cols).index(auto_select(pr_cols, ['igst', 'integrated'])) if auto_select(pr_cols, ['igst', 'integrated']) else 0)
        pr_cgst = st.selectbox("CGST", ['-- None --'] + pr_cols, index=(['-- None --'] + pr_cols).index(auto_select(pr_cols, ['cgst', 'central'])) if auto_select(pr_cols, ['cgst', 'central']) else 0)
        pr_sgst = st.selectbox("SGST / UTGST", ['-- None --'] + pr_cols, index=(['-- None --'] + pr_cols).index(auto_select(pr_cols, ['sgst', 'utgst'])) if auto_select(pr_cols, ['sgst', 'utgst']) else 0)
    
    with map_col2:
        st.markdown("**GSTR-2B columns**")
        g_gstin = st.selectbox("Supplier GSTIN", gstr_cols, key="g_gstin", index=gstr_cols.index(auto_select(gstr_cols, ['gstin', 'supplier gstin', 'ctin'])) if auto_select(gstr_cols, ['gstin', 'supplier gstin', 'ctin']) in gstr_cols else 0)
        g_inv = st.selectbox("Invoice Number", gstr_cols, key="g_inv", index=gstr_cols.index(auto_select(gstr_cols, ['invoice', 'inv no', 'inum'])) if auto_select(gstr_cols, ['invoice', 'inv no', 'inum']) in gstr_cols else 0)
        g_date = st.selectbox("Invoice Date (optional)", ['-- None --'] + gstr_cols, key="g_date", index=0)
        g_taxable = st.selectbox("Taxable Value", gstr_cols, key="g_taxable", index=gstr_cols.index(auto_select(gstr_cols, ['taxable', 'val'])) if auto_select(gstr_cols, ['taxable', 'val']) in gstr_cols else 0)
        g_igst = st.selectbox("IGST", ['-- None --'] + gstr_cols, key="g_igst", index=(['-- None --'] + gstr_cols).index(auto_select(gstr_cols, ['igst', 'iamt'])) if auto_select(gstr_cols, ['igst', 'iamt']) else 0)
        g_cgst = st.selectbox("CGST", ['-- None --'] + gstr_cols, key="g_cgst", index=(['-- None --'] + gstr_cols).index(auto_select(gstr_cols, ['cgst', 'camt'])) if auto_select(gstr_cols, ['cgst', 'camt']) else 0)
        g_sgst = st.selectbox("SGST / UTGST", ['-- None --'] + gstr_cols, key="g_sgst", index=(['-- None --'] + gstr_cols).index(auto_select(gstr_cols, ['sgst', 'samt'])) if auto_select(gstr_cols, ['sgst', 'samt']) else 0)
    
    if st.button("🚀 Run 3-Level Reconciliation", type="primary", use_container_width=True):
        with st.spinner("Jarvis GST is running 3-level matching..."):
            pr_map = {
                'gstin': pr_gstin, 'invoice_no': pr_inv,
                'invoice_date': pr_date if pr_date != '-- None --' else None,
                'taxable': pr_taxable,
                'igst': pr_igst if pr_igst != '-- None --' else None,
                'cgst': pr_cgst if pr_cgst != '-- None --' else None,
                'sgst': pr_sgst if pr_sgst != '-- None --' else None,
            }
            g_map = {
                'gstin': g_gstin, 'invoice_no': g_inv,
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
                fuzzy_threshold=fuzzy_th
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
        st.subheader("📊 Reconciliation Summary")
        
        exact_cnt = len(matched[matched['Match Type'] == 'Exact']) if not matched.empty else 0
        fuzzy_cnt = len(matched[matched['Match Type'] == 'Fuzzy']) if not matched.empty else 0
        near_cnt = len(matched[matched['Match Type'] == 'Near Match']) if not matched.empty else 0
        mismatch_cnt = len(matched[matched['Status'] == 'Value Mismatch']) if not matched.empty else 0
        
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("🎯 Exact", exact_cnt)
        m2.metric("🔍 Fuzzy", fuzzy_cnt)
        m3.metric("📋 Correction", near_cnt)
        m4.metric("⚠️ Mismatch", mismatch_cnt)
        m5.metric("🔴 Missing 2B", len(missing))
        m6.metric("🟡 Extra 2B", len(extra))
        
        excel_bytes = to_excel_download(matched, missing, extra)
        st.download_button(
            label="📥 Download Full Report (Excel with Separate Level Sheets)",
            data=excel_bytes,
            file_name=f"Jarvis_GST_Reconciliation_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
        
        r1, r2, r3, r4, r5, r6 = st.tabs(["🎯 Exact Match", "🔍 Fuzzy Match", "📋 Invoice Correction", "⚠️ Value Mismatch", "Missing in 2B", "Extra in 2B"])
        
        with r1:
            df_exact = matched[matched['Match Type'] == 'Exact'] if not matched.empty else pd.DataFrame()
            if not df_exact.empty:
                st.dataframe(df_exact, use_container_width=True, height=400)
            else:
                st.info("No exact match invoices.")
        
        with r2:
            df_fuzzy = matched[matched['Match Type'] == 'Fuzzy'] if not matched.empty else pd.DataFrame()
            if not df_fuzzy.empty:
                st.dataframe(df_fuzzy, use_container_width=True, height=400)
            else:
                st.info("No fuzzy match invoices.")

        with r3:
            df_near = matched[matched['Match Type'] == 'Near Match'] if not matched.empty else pd.DataFrame()
            if not df_near.empty:
                st.dataframe(df_near, use_container_width=True, height=400)
            else:
                st.success("No invoice corrections needed.")
        
        with r4:
            df_mismatch = matched[matched['Status'] == 'Value Mismatch'] if not matched.empty else pd.DataFrame()
            if not df_mismatch.empty:
                st.dataframe(df_mismatch, use_container_width=True, height=400)
            else:
                st.success("No value mismatches.")
        
        with r5:
            if not missing.empty:
                st.dataframe(missing, use_container_width=True, height=400)
            else:
                st.success("No missing invoices.")
        
        with r6:
            if not extra.empty:
                st.dataframe(extra, use_container_width=True, height=400)
            else:
                st.success("No extra invoices in 2B.")

st.markdown("---")
st.caption("Jarvis GST • Local Reconciliation Platform")
