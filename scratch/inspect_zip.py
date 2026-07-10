import zipfile

with zipfile.ZipFile('easyocr_lmdb_dataset_ready.zip', 'r') as z:
    for name in z.namelist()[:15]:
        print(name)
