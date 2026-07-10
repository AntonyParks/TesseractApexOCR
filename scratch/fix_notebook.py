from pathlib import Path

path = Path("train_trocr_colab.ipynb")
content = path.read_text(encoding="utf-8")
old_line = '"QUALITY = {\'high\', \'medium\', \'low\'}\\n",'
new_line = '"QUALITY = {\'confirmed\', \'correction\'}\\n",'

if old_line in content:
    content = content.replace(old_line, new_line)
    path.write_text(content, encoding="utf-8")
    print("Notebook updated successfully.")
else:
    print("Old line not found in notebook.")
