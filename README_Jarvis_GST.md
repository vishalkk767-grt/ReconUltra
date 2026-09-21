# 🤖 Jarvis GST Reconciliation Platform

**Full ownership. 100% local. Your data never leaves your computer.**

Jarvis automatically matches your **Purchase Register** with **GSTR-2B** (B2B invoices) using exact + smart fuzzy matching.

---

## Features

- Upload Purchase Register (Excel / CSV)
- Upload GSTR-2B (Excel / CSV / JSON)
- Auto column detection + manual mapping
- Exact match on GSTIN + Invoice Number
- Fuzzy matching for slight invoice number differences
- Amount tolerance check (Taxable + IGST/CGST/SGST)
- Clear categories:
  - Fully Matched
  - Value Mismatch
  - Missing in GSTR-2B (ITC risk)
  - Extra in GSTR-2B
- One-click Excel report download
- Completely offline after installation

---

## How to Run (Windows / Mac / Linux)

### 1. Install Python
Download from https://www.python.org (3.10 or higher recommended)

### 2. Open terminal / command prompt in this folder and run:

```bash
pip install -r requirements.txt
```

### 3. Start Jarvis:

```bash
streamlit run jarvis_gst.py
```

Browser will open automatically at `http://localhost:8501`

---

## Tips for best matching

- Keep Supplier GSTIN and Invoice Number columns clean
- Prefer separate CGST / SGST / IGST columns
- Adjust **Amount Tolerance** and **Fuzzy Threshold** from the sidebar if needed

---

## Ownership

This entire source code is yours.  
You can modify, redistribute, or use it commercially without any restriction from the creator.

---

Made for Indian GST compliance.
