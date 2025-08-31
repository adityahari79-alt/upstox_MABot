import streamlit as st
try:
    from smartapi import SmartConnect
    st.write("SmartAPI imported successfully")
except ModuleNotFoundError:
    st.error("Failed to import SmartAPI")
