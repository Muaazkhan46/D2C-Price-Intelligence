import pandas as pd
df = pd.read_csv("data/parsed/variants_daily.csv")
fox = df[(df.brand=="Foxtale") & df.is_freebie].drop_duplicates("product_title")
print(fox[["product_title","product_type","price"]].head(20))