import streamlit as st
import pandas as pd
import re
import io

# Ensure pdfplumber is available for PDF handling
try:
    import pdfplumber
except ImportError:
    st.error("Please install pdfplumber: pip install pdfplumber")

st.set_page_config(page_title="Print Audit System v4", layout="wide")

# --- Session State Management ---
if 'restart' not in st.session_state:
    st.session_state.restart = False

def restart_app():
    st.session_state.clear()
    st.rerun()

st.title("🖨️ Printing Sales vs. Output Audit")
st.markdown("Reconcile POS sales with Printer logs, including detailed job matching and anonymous print tracking.")

# --- Functions ---
def load_any_file(uploaded_file):
    if uploaded_file is None:
        return None
    file_extension = uploaded_file.name.split('.')[-1].lower()
    try:
        if file_extension == 'csv':
            return pd.read_csv(uploaded_file)
        elif file_extension in ['xlsx', 'xls']:
            return pd.read_excel(uploaded_file)
        elif file_extension == 'pdf':
            with pdfplumber.open(uploaded_file) as pdf:
                all_data = []
                for page in pdf.pages:
                    table = page.extract_table()
                    if table:
                        all_data.extend(table)
                if not all_data:
                    st.error(f"No tables found in PDF: {uploaded_file.name}")
                    return None
                df = pd.DataFrame(all_data[1:], columns=all_data[0])
                return df
    except Exception as e:
        st.error(f"Error reading {uploaded_file.name}: {e}")
        return None

# --- Sidebar: Control Panel ---
st.sidebar.header("📁 Data Sources")
pos_file = st.sidebar.file_uploader("Upload POS File", type=["csv", "xlsx", "pdf"], key="pos_loader")
fuji_file = st.sidebar.file_uploader("Upload Fuji Printer File", type=["csv", "xlsx", "pdf"], key="fuji_loader")

st.sidebar.markdown("---")
if st.sidebar.button("🔄 Restart App", on_click=restart_app):
    st.stop()

if st.sidebar.button("❌ Exit"):
    st.sidebar.warning("You can now close this browser tab.")
    st.stop()

if pos_file and fuji_file:
    pos_df = load_any_file(pos_file)
    fuji_df = load_any_file(fuji_file)

    if pos_df is not None and fuji_df is not None:
        # --- Processing ---
        # 1. POS Prep
        # Extract DR number (e.g., DR15322 -> 15322)
        pos_df['DR_Num'] = pos_df['Invoice No.'].astype(str).str.extract(r'DR(\d+)').astype(float)
        
        # Filter for Digital Print only, excluding proofing/stickers
        pos_print = pos_df[pos_df['Item Name'].str.contains('DIGITAL PRINT', case=False, na=False)].copy()
        pos_print = pos_print[~pos_print['Item Name'].str.contains('PROOF|STICKER', case=False, na=False)]

        def calc_pages(row):
            qty_str = str(row['Sales Qty']).replace(',', '').strip()
            qty = pd.to_numeric(qty_str, errors='coerce') or 0
            return qty * 2 if '2 SIDES' in str(row['Item Name']).upper() else qty
        
        pos_print['Expected_Pages'] = pos_print.apply(calc_pages, axis=1)
        pos_grouped = pos_print.groupby('DR_Num').agg({
            'Invoice No.': 'first',
            'Customer Name': 'first',
            'Expected_Pages': 'sum'
        }).reset_index()

        # 2. Fuji Prep
        fuji_df['Printed Pages'] = pd.to_numeric(fuji_df['Printed Pages'], errors='coerce').fillna(0)
        # Extract DR number with optional space
        fuji_df['DR_Num'] = fuji_df['Job Name'].astype(str).str.extract(r'DR\s*(\d+)').astype(float)
        
        # 3. Categorize Fuji Prints
        # Anonymous: Jobs without a DR number
        anonymous_raw = fuji_df[fuji_df['DR_Num'].isnull()].copy()
        
        # Aggregate Anonymous Prints by Job Name and Artist (Owner)
        anonymous_summary = anonymous_raw.groupby(['Job Name', 'Owner']).agg({
            'Printed Pages': 'sum',
            'Recorded Date/Time': 'max'
        }).reset_index()
        anonymous_summary.rename(columns={'Owner': 'Artist Name'}, inplace=True)
        anonymous_summary = anonymous_summary.sort_values(by='Printed Pages', ascending=False)

        # DR Jobs
        fuji_with_dr = fuji_df[fuji_df['DR_Num'].notnull()]
        fuji_grouped = fuji_with_dr.groupby('DR_Num')['Printed Pages'].sum().reset_index()

        # 4. Reconciliation
        merged = pd.merge(pos_grouped, fuji_grouped, on='DR_Num', how='outer', indicator=True)
        unprinted = merged[merged['_merge'] == 'left_only'].copy()
        matched = merged[merged['_merge'] == 'both'].copy()
        matched['Diff'] = matched['Printed Pages'] - matched['Expected_Pages']
        mismatches = matched[matched['Diff'] != 0].copy()

        # --- UI Dashboard ---
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Billed Invoices", len(pos_grouped))
        m2.metric("Unprinted (Risk)", len(unprinted))
        m3.metric("Qty Mismatches", len(mismatches))
        m4.metric("Anon. Unique Jobs", len(anonymous_summary))

        tab1, tab2, tab3 = st.tabs(["⚠️ Unprinted Invoices", "⚖️ Quantity Mismatches", "❓ Anonymous Prints"])

        with tab1:
            st.subheader("Billed in POS but not found in Printer Log")
            st.dataframe(unprinted[['Invoice No.', 'Customer Name', 'Expected_Pages']], use_container_width=True)

        with tab2:
            st.subheader("Variance between Sold vs Printed")
            st.dataframe(mismatches[['Invoice No.', 'Customer Name', 'Expected_Pages', 'Printed Pages', 'Diff']], use_container_width=True)
            
            # Proof Section for large discrepancies
            large_mismatches = mismatches[mismatches['Diff'].abs() > 10]
            if not large_mismatches.empty:
                st.markdown("---")
                st.error("🔍 **Printer Log Proof (Detailed Breakdown for Diff > 10)**")
                # Create labels for selectbox
                labels = large_mismatches.apply(lambda r: f"{r['Invoice No.']} - {r['Customer Name']} (Diff: {r['Diff']})", axis=1).tolist()
                dr_map = dict(zip(labels, large_mismatches['DR_Num']))
                
                selected_label = st.selectbox("Select a mismatched job to view proof:", labels)
                selected_dr = dr_map[selected_label]
                
                if selected_dr:
                    proof = fuji_with_dr[fuji_with_dr['DR_Num'] == selected_dr][['Recorded Date/Time', 'Job Name', 'Printed Pages', 'Owner']]
                    st.table(proof)

        with tab3:
            st.subheader("Fuji Prints without DR Number (Grouped by Job Name)")
            st.info("These jobs were printed without a DR number in the title. Totals are summed for identical Job Names.")
            st.dataframe(anonymous_summary[['Job Name', 'Artist Name', 'Printed Pages', 'Recorded Date/Time']], use_container_width=True)

        # Download sidebar
        st.sidebar.markdown("---")
        st.sidebar.download_button("💾 Download Mismatch CSV", mismatches.to_csv(index=False), "audit_mismatches.csv")
        st.sidebar.download_button("💾 Download Anonymous CSV", anonymous_summary.to_csv(index=False), "anonymous_prints.csv")
else:
    st.info("Please upload your POS and Fuji files to generate the audit.")