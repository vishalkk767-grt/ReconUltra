"""
Jarvis GST Reconciliation Platform
----------------------------------
Full local ownership. Runs entirely on your computer.
Purchase Register vs GSTR-2B matching for B2B invoices.
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

# Custom CSS for clean look
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
    .metric-card {
        background: #F8FAFC;
        padding: 1rem;
        border-radius: 10px;
        border-left: 4px solid #2563EB;
    }
    .stDownloadButton button {
        background-color: #2563EB;
        color: white;
        border-radius: 8px;
    }
</style>
""", unsafe_allow_html=True)

# --------------------------
# Helper Functions
# --------------------------

def clean_gstin(val):
    if pd.isna(val):
        return ""
    return str(val).strip().upper().replace(" ", "")

def clean_invoice(val):
    if pd.isna(val):
        return ""
    s = str(val).strip().upper()
    # Remove common noise but keep structure
    s = re.sub(r'[\s\.\-_/\\]', '', s)
    return s

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
    """Create standardized columns from user mapping"""
    result = pd.DataFrame()
    result['gstin'] = df[mapping['gstin']].apply(clean_gstin)
    result['invoice_no'] = df[mapping['invoice_no']].apply(clean_invoice)
    result['invoice_no_raw'] = df[mapping['invoice_no']].astype(str).str.strip()
    result['invoice_date'] = df[mapping['invoice_date']].apply(parse_date) if mapping.get('invoice_date') else None
    result['taxable'] = df[mapping['taxable']].apply(clean_amount) if mapping.get('taxable') else 0.0
    result['igst'] = df[mapping.get('igst', mapping['taxable'])].apply(clean_amount) if mapping.get('igst') else 0.0
    result['cgst'] = df[mapping.get('cgst', mapping['taxable'])].apply(clean_amount) if mapping.get('cgst') else 0.0
    result['sgst'] = df[mapping.get('sgst', mapping['taxable'])].apply(clean_amount) if mapping.get('sgst') else 0.0
    result['total_tax'] = result['igst'] + result['cgst'] + result['sgst']
    
    # Keep original row index for later
    result['orig_idx'] = df.index
    return result

def create_match_key(gstin, inv):
    return f"{gstin}|{inv}"

def fuzzy_invoice_match(inv, candidates, threshold=85):
    """Find best fuzzy match for invoice number"""
    if not candidates:
        return None, 0
    match = process.extractOne(inv, candidates, scorer=fuzz.ratio)
    if match and match[1] >= threshold:
        return match[0], match[1]
    return None, 0

def reconcile(pr_df, gstr_df, amount_tolerance=2.0, fuzzy_threshold=88):
    """
    Main reconciliation engine
    Returns categorized dataframes
    """
    pr = pr_df.copy()
    gstr = gstr_df.copy()
    
    pr['key'] = pr.apply(lambda r: create_match_key(r['gstin'], r['invoice_no']), axis=1)
    gstr['key'] = gstr.apply(lambda r: create_match_key(r['gstin'], r['invoice_no']), axis=1)
    
    # Exact key match
    pr_keys = set(pr['key'])
    gstr_keys = set(gstr['key'])
    
    exact_matched_keys = pr_keys & gstr_keys
    
    # For remaining, try fuzzy per GSTIN
    remaining_pr = pr[~pr['key'].isin(exact_matched_keys)].copy()
    remaining_gstr = gstr[~gstr['key'].isin(exact_matched_keys)].copy()
    
    fuzzy_matches = []  # list of (pr_idx, gstr_idx, score)
    
    for gstin, pr_group in remaining_pr.groupby('gstin'):
        gstr_group = remaining_gstr[remaining_gstr['gstin'] == gstin]
        if gstr_group.empty:
            continue
        gstr_invs = gstr_group['invoice_no'].tolist()
        gstr_idx_map = dict(zip(gstr_group['invoice_no'], gstr_group.index))
        
        used_gstr = set()
        for pr_idx, row in pr_group.iterrows():
            best_inv, score = fuzzy_invoice_match(row['invoice_no'], 
                                                   [i for i in gstr_invs if i not in used_gstr],
                                                   threshold=fuzzy_threshold)
            if best_inv:
                gstr_idx = gstr_idx_map[best_inv]
                fuzzy_matches.append((pr_idx, gstr_idx, score))
                used_gstr.add(best_inv)
    
    # Build result categories
    matched_rows = []
    mismatch_rows = []
    
    # Exact matches
    for key in exact_matched_keys:
        pr_row = pr[pr['key'] == key].iloc[0]
        gstr_row = gstr[gstr['key'] == key].iloc[0]
        
        tax_diff = abs(pr_row['total_tax'] - gstr_row['total_tax'])
        taxable_diff = abs(pr_row['taxable'] - gstr_row['taxable'])
        
        status = "Fully Matched" if (tax_diff <= amount_tolerance and taxable_diff <= amount_tolerance) else "Value Mismatch"
        
        matched_rows.append({
            'Status': status,
            'Match Type': 'Exact',
            'GSTIN': pr_row['gstin'],
            'Invoice No (Books)': pr_row['invoice_no_raw'],
            'Invoice No (2B)': gstr_row['invoice_no_raw'],
            'Taxable (Books)': pr_row['taxable'],
            'Taxable (2B)': gstr_row['taxable'],
            'Tax Diff': round(pr_row['total_tax'] - gstr_row['total_tax'], 2),
            'IGST (Books)': pr_row['igst'],
            'IGST (2B)': gstr_row['igst'],
            'CGST (Books)': pr_row['cgst'],
            'CGST (2B)': gstr_row['cgst'],
            'SGST (Books)': pr_row['sgst'],
            'SGST (2B)': gstr_row['sgst'],
            'Fuzzy Score': 100
        })
    
    # Fuzzy matches
    for pr_idx, gstr_idx, score in fuzzy_matches:
        pr_row = pr.loc[pr_idx]
        gstr_row = gstr.loc[gstr_idx]
        
        tax_diff = abs(pr_row['total_tax'] - gstr_row['total_tax'])
        taxable_diff = abs(pr_row['taxable'] - gstr_row['taxable'])
        
        status = "Fully Matched (Fuzzy)" if (tax_diff <= amount_tolerance and taxable_diff <= amount_tolerance) else "Value Mismatch (Fuzzy)"
        
        matched_rows.append({
            'Status': status,
            'Match Type': 'Fuzzy',
            'GSTIN': pr_row['gstin'],
            'Invoice No (Books)': pr_row['invoice_no_raw'],
            'Invoice No (2B)': gstr_row['invoice_no_raw'],
            'Taxable (Books)': pr_row['taxable'],
            'Taxable (2B)': gstr_row['taxable'],
            'Tax Diff': round(pr_row['total_tax'] - gstr_row['total_tax'], 2),
            'IGST (Books)': pr_row['igst'],
            'IGST (2B)': gstr_row['igst'],
            'CGST (Books)': pr_row['cgst'],
            'CGST (2B)': gstr_row['cgst'],
            'SGST (Books)': pr_row['sgst'],
            'SGST (2B)': gstr_row['sgst'],
            'Fuzzy Score': score
        })
    
    matched_df = pd.DataFrame(matched_rows)
    
    # Missing in 2B (in PR but not matched)
    matched_pr_idxs = set()
    for key in exact_matched_keys:
        matched_pr_idxs.add(pr[pr['key'] == key].index[0])
    for pr_idx, _, _ in fuzzy_matches:
        matched_pr_idxs.add(pr_idx)
    
    missing_in_2b = pr[~pr.index.isin(matched_pr_idxs)].copy()
    missing_in_2b = missing_in_2b[['gstin', 'invoice_no_raw', 'taxable', 'igst', 'cgst', 'sgst', 'total_tax']]
    missing_in_2b.columns = ['GSTIN', 'Invoice No', 'Taxable', 'IGST', 'CGST', 'SGST', 'Total Tax']
    
    # Extra in 2B (in GSTR but not matched)
    matched_gstr_idxs = set()
    for key in exact_matched_keys:
        matched_gstr_idxs.add(gstr[gstr['key'] == key].index[0])
    for _, gstr_idx, _ in fuzzy_matches:
        matched_gstr_idxs.add(gstr_idx)
    
    extra_in_2b = gstr[~gstr.index.isin(matched_gstr_idxs)].copy()
    extra_in_2b = extra_in_2b[['gstin', 'invoice_no_raw', 'taxable', 'igst', 'cgst', 'sgst', 'total_tax']]
    extra_in_2b.columns = ['GSTIN', 'Invoice No', 'Taxable', 'IGST', 'CGST', 'SGST', 'Total Tax']
    
    return matched_df, missing_in_2b, extra_in_2b

def to_excel_download(matched, missing, extra):
    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        if not matched.empty:
            matched.to_excel(writer, sheet_name='Matched', index=False)
        if not missing.empty:
            missing.to_excel(writer, sheet_name='Missing in 2B', index=False)
        if not extra.empty:
            extra.to_excel(writer, sheet_name='Extra in 2B', index=False)
        
        # Summary sheet
        summary_data = {
            'Category': ['Fully Matched', 'Value Mismatch', 'Missing in GSTR-2B', 'Extra in GSTR-2B'],
            'Count': [
                len(matched[matched['Status'].str.contains('Fully Matched')]) if not matched.empty else 0,
                len(matched[matched['Status'].str.contains('Mismatch')]) if not matched.empty else 0,
                len(missing),
                len(extra)
            ]
        }
        pd.DataFrame(summary_data).to_excel(writer, sheet_name='Summary', index=False)
    
    output.seek(0)
    return output

def try_parse_gstr2b_json(file):
    """Basic GSTR-2B JSON parser for B2B section"""
    try:
        data = json.load(file)
        rows = []
        
        # Common paths in GSTR-2B JSON
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
    st.markdown("### How to use")
    st.markdown("""
    1. Upload **Purchase Register** Excel  
    2. Upload **GSTR-2B** Excel or JSON  
    3. Map columns (auto-detected where possible)  
    4. Click **Run Reconciliation**  
    5. Download full report  
    """)
    
    st.markdown("---")
    st.info("🔒 100% Local • Your data never leaves your computer")

# --------------------------
# Main UI
# --------------------------
st.markdown('<div class="main-header">🤖 Jarvis GST</div>', unsafe_allow_html=True)
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
        # Load files
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
        
        # Column mapping
        st.markdown("### 3️⃣ Column Mapping")
        st.caption("Select the correct columns from your files. Common names are auto-selected when possible.")
        
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
            pr_gstin = st.selectbox("Supplier GSTIN", pr_cols, 
                                    index=pr_cols.index(auto_select(pr_cols, ['gstin', 'gstin of supplier', 'supplier gstin', 'ctin'])) 
                                    if auto_select(pr_cols, ['gstin', 'gstin of supplier', 'supplier gstin', 'ctin']) in pr_cols else 0)
            pr_inv = st.selectbox("Invoice Number", pr_cols,
                                  index=pr_cols.index(auto_select(pr_cols, ['invoice', 'inv no', 'document no', 'bill no'])) 
                                  if auto_select(pr_cols, ['invoice', 'inv no', 'document no', 'bill no']) in pr_cols else 0)
            pr_date = st.selectbox("Invoice Date (optional)", ['-- None --'] + pr_cols,
                                   index=0)
            pr_taxable = st.selectbox("Taxable Value", pr_cols,
                                      index=pr_cols.index(auto_select(pr_cols, ['taxable', 'taxable value', 'taxable amt'])) 
                                      if auto_select(pr_cols, ['taxable', 'taxable value', 'taxable amt']) in pr_cols else 0)
            pr_igst = st.selectbox("IGST", ['-- None --'] + pr_cols,
                                   index=(['-- None --'] + pr_cols).index(auto_select(pr_cols, ['igst', 'integrated'])) 
                                   if auto_select(pr_cols, ['igst', 'integrated']) else 0)
            pr_cgst = st.selectbox("CGST", ['-- None --'] + pr_cols,
                                   index=(['-- None --'] + pr_cols).index(auto_select(pr_cols, ['cgst', 'central'])) 
                                   if auto_select(pr_cols, ['cgst', 'central']) else 0)
            pr_sgst = st.selectbox("SGST / UTGST", ['-- None --'] + pr_cols,
                                   index=(['-- None --'] + pr_cols).index(auto_select(pr_cols, ['sgst', 'utgst', 'state'])) 
                                   if auto_select(pr_cols, ['sgst', 'utgst', 'state']) else 0)
        
        with map_col2:
            st.markdown("**GSTR-2B columns**")
            g_gstin = st.selectbox("Supplier GSTIN", gstr_cols, key="g_gstin",
                                   index=gstr_cols.index(auto_select(gstr_cols, ['gstin', 'gstin of supplier', 'supplier gstin', 'ctin'])) 
                                   if auto_select(gstr_cols, ['gstin', 'gstin of supplier', 'supplier gstin', 'ctin']) in gstr_cols else 0)
            g_inv = st.selectbox("Invoice Number", gstr_cols, key="g_inv",
                                 index=gstr_cols.index(auto_select(gstr_cols, ['invoice', 'inv no', 'document no', 'inum'])) 
                                 if auto_select(gstr_cols, ['invoice', 'inv no', 'document no', 'inum']) in gstr_cols else 0)
            g_date = st.selectbox("Invoice Date (optional)", ['-- None --'] + gstr_cols, key="g_date",
                                  index=0)
            g_taxable = st.selectbox("Taxable Value", gstr_cols, key="g_taxable",
                                     index=gstr_cols.index(auto_select(gstr_cols, ['taxable', 'taxable value', 'val'])) 
                                     if auto_select(gstr_cols, ['taxable', 'taxable value', 'val']) in gstr_cols else 0)
            g_igst = st.selectbox("IGST", ['-- None --'] + gstr_cols, key="g_igst",
                                  index=(['-- None --'] + gstr_cols).index(auto_select(gstr_cols, ['igst', 'integrated', 'iamt'])) 
                                  if auto_select(gstr_cols, ['igst', 'integrated', 'iamt']) else 0)
            g_cgst = st.selectbox("CGST", ['-- None --'] + gstr_cols, key="g_cgst",
                                  index=(['-- None --'] + gstr_cols).index(auto_select(gstr_cols, ['cgst', 'central', 'camt'])) 
                                  if auto_select(gstr_cols, ['cgst', 'central', 'camt']) else 0)
            g_sgst = st.selectbox("SGST / UTGST", ['-- None --'] + gstr_cols, key="g_sgst",
                                  index=(['-- None --'] + gstr_cols).index(auto_select(gstr_cols, ['sgst', 'utgst', 'state', 'samt'])) 
                                  if auto_select(gstr_cols, ['sgst', 'utgst', 'state', 'samt']) else 0)
        
        # Run button
        if st.button("🚀 Run Reconciliation", type="primary", use_container_width=True):
            with st.spinner("Jarvis is matching invoices..."):
                pr_map = {
                    'gstin': pr_gstin,
                    'invoice_no': pr_inv,
                    'invoice_date': pr_date if pr_date != '-- None --' else None,
                    'taxable': pr_taxable,
                    'igst': pr_igst if pr_igst != '-- None --' else None,
                    'cgst': pr_cgst if pr_cgst != '-- None --' else None,
                    'sgst': pr_sgst if pr_sgst != '-- None --' else None,
                }
                g_map = {
                    'gstin': g_gstin,
                    'invoice_no': g_inv,
                    'invoice_date': g_date if g_date != '-- None --' else None,
                    'taxable': g_taxable,
                    'igst': g_igst if g_igst != '-- None --' else None,
                    'cgst': g_cgst if g_cgst != '-- None --' else None,
                    'sgst': g_sgst if g_sgst != '-- None --' else None,
                }
                
                pr_norm = normalize_df(pr_raw, pr_map)
                gstr_norm = normalize_df(gstr_raw, g_map)
                
                # Remove empty GSTIN or Invoice
                pr_norm = pr_norm[(pr_norm['gstin'] != '') & (pr_norm['invoice_no'] != '')]
                gstr_norm = gstr_norm[(gstr_norm['gstin'] != '') & (gstr_norm['invoice_no'] != '')]
                
                matched, missing, extra = reconcile(pr_norm, gstr_norm, 
                                                    amount_tolerance=amount_tol,
                                                    fuzzy_threshold=fuzzy_th)
                
                st.session_state['matched'] = matched
                st.session_state['missing'] = missing
                st.session_state['extra'] = extra
                st.session_state['done'] = True
        
        # Show results
        if st.session_state.get('done'):
            matched = st.session_state['matched']
            missing = st.session_state['missing']
            extra = st.session_state['extra']
            
            st.markdown("---")
            st.subheader("📊 Reconciliation Summary")
            
            fully = len(matched[matched['Status'].str.contains('Fully Matched')]) if not matched.empty else 0
            mismatch = len(matched[matched['Status'].str.contains('Mismatch')]) if not matched.empty else 0
            
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("✅ Fully Matched", fully)
            m2.metric("⚠️ Value Mismatch", mismatch)
            m3.metric("🔴 Missing in 2B", len(missing), help="Invoices in your books but not in GSTR-2B")
            m4.metric("🟡 Extra in 2B", len(extra), help="In GSTR-2B but not found in your books")
            
            # Download
            excel_bytes = to_excel_download(matched, missing, extra)
            st.download_button(
                label="📥 Download Full Report (Excel)",
                data=excel_bytes,
                file_name=f"Jarvis_GST_Reconciliation_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
            
            # Detailed tabs
            r1, r2, r3 = st.tabs(["Matched Invoices", "Missing in GSTR-2B", "Extra in GSTR-2B"])
            
            with r1:
                if not matched.empty:
                    st.dataframe(matched, use_container_width=True, height=400)
                else:
                    st.info("No matched invoices found.")
            
            with r2:
                if not missing.empty:
                    st.warning("These invoices are in your Purchase Register but missing in GSTR-2B. Follow up with suppliers.")
                    st.dataframe(missing, use_container_width=True, height=400)
                else:
                    st.success("No missing invoices. Great!")
            
            with r3:
                if not extra.empty:
                    st.info("These appear in GSTR-2B but were not found in your books. Check if you missed booking them.")
                    st.dataframe(extra, use_container_width=True, height=400)
                else:
                    st.success("No extra invoices in 2B.")

with tab2:
    st.markdown("""
    ### How Jarvis matches data
    
    1. **Normalization**
       - GSTIN → uppercase, remove spaces
       - Invoice Number → uppercase, remove spaces / dashes / slashes for matching
    
    2. **Exact Match**
       - First tries GSTIN + cleaned Invoice Number
    
    3. **Fuzzy Match**
       - If exact fails, for same GSTIN it looks for similar invoice numbers
       - Uses advanced string similarity (you control the threshold)
    
    4. **Amount Check**
       - Compares Taxable Value + IGST + CGST + SGST
       - Difference within tolerance = Fully Matched
       - Beyond tolerance = Value Mismatch
    
    ### Recommended columns in Purchase Register
    - Supplier GSTIN
    - Invoice Number
    - Invoice Date
    - Taxable Value
    - IGST / CGST / SGST (separate columns preferred)
    
    ### GSTR-2B
    - Download Excel or JSON from GST portal
    - B2B section is used for matching
    """)

with tab3:
    st.markdown("""
    ### About Jarvis GST
    
    - **Name**: Jarvis GST Reconciliation Platform  
    - **Ownership**: 100% yours  
    - **Privacy**: All processing happens on your computer. No data is uploaded anywhere.  
    - **Tech**: Python + Streamlit + RapidFuzz + Pandas  
    
    You can modify the code, add new features, or white-label it.
    
    ---
    Built for Indian GST compliance (GSTR-2B vs Purchase Register).
    """)

# Footer
st.markdown("---")
st.caption("Jarvis GST • Local Reconciliation Platform • Your data stays with you")
