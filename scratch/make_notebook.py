import json

notebook = {
  "nbformat": 4,
  "nbformat_minor": 0,
  "metadata": {
    "colab": {
      "provenance": []
    },
    "kernelspec": {
      "name": "python3",
      "display_name": "Python 3"
    },
    "language_info": {
      "name": "python"
    }
  },
  "cells": [
    {
      "cell_type": "markdown",
      "metadata": {"id": "markdown-1"},
      "source": ["# 1. Setup Environment"]
    },
    {
      "cell_type": "code",
      "metadata": {"id": "code-1"},
      "source": [
        "!git clone https://github.com/clovaai/deep-text-recognition-benchmark\n",
        "%cd deep-text-recognition-benchmark\n",
        "!pip install lmdb pillow torchvision nltk natsort torch"
      ],
      "execution_count": None,
      "outputs": []
    },
    {
      "cell_type": "markdown",
      "metadata": {"id": "markdown-2"},
      "source": ["# 2. Upload and Extract Dataset"]
    },
    {
      "cell_type": "code",
      "metadata": {"id": "code-2"},
      "source": [
        "from google.colab import files\n",
        "\n",
        "print(\"Please upload easyocr_lmdb_dataset_ready.zip...\")\n",
        "uploaded = files.upload()\n",
        "\n",
        "if 'easyocr_lmdb_dataset_ready.zip' not in uploaded:\n",
        "    print(\"ERROR: Please upload easyocr_lmdb_dataset_ready.zip first!\")\n",
        "else:\n",
        "    print(\"Upload complete! Extracting...\")\n",
        "    !unzip -q -o easyocr_lmdb_dataset_ready.zip\n",
        "    !mkdir -p dataset\n",
        "    !mv train_lmdb dataset/ 2>/dev/null || true\n",
        "    !mv val_lmdb dataset/ 2>/dev/null || true\n",
        "    print(\"Extracted and moved to dataset/!\")"
      ],
      "execution_count": None,
      "outputs": []
    },
    {
      "cell_type": "markdown",
      "metadata": {"id": "markdown-3"},
      "source": ["# 3. Download original weights, Patch bug, and Fine-Tune"]
    },
    {
      "cell_type": "code",
      "metadata": {"id": "code-3"},
      "source": [
        "import subprocess\n",
        "import sys\n",
        "import os\n",
        "import urllib.request\n",
        "import zipfile\n",
        "\n",
        "# 1. Download the actual pre-trained english_g2 model\n",
        "if not os.path.exists(\"english_g2.pth\"):\n",
        "    print(\"Downloading pretrained english_g2 model...\")\n",
        "    urllib.request.urlretrieve(\"https://github.com/JaidedAI/EasyOCR/releases/download/v1.3/english_g2.zip\", \"english_g2.zip\")\n",
        "    with zipfile.ZipFile(\"english_g2.zip\", 'r') as zip_ref:\n",
        "        zip_ref.extractall(\".\")\n",
        "    print(\"Extracted english_g2.pth!\")\n",
        "\n",
        "# 2. Patch train.py so it doesn't overwrite our custom characters and works on CPU/GPU\n",
        "print(\"Patching train.py...\")\n",
        "with open(\"train.py\", \"r\", encoding=\"utf-8\") as f:\n",
        "    code = f.read()\n",
        "# Disable the hardcoded overwrite\n",
        "code = code.replace(\"opt.character = string.printable[:-6]\", \"pass # opt.character OVERWRITE DISABLED\")\n",
        "# Fix CUDA deserialization issue if running on CPU\n",
        "code = code.replace(\"torch.load(opt.saved_model)\", \"torch.load(opt.saved_model, map_location=device)\")\n",
        "with open(\"train.py\", \"w\", encoding=\"utf-8\") as f:\n",
        "    f.write(code)\n",
        "\n",
        "# 3. Patch dataset.py to fix PyTorch _accumulate import error!\n",
        "print(\"Patching dataset.py...\")\n",
        "with open(\"dataset.py\", \"r\", encoding=\"utf-8\") as f:\n",
        "    code = f.read()\n",
        "code = code.replace(\"from torch._utils import _accumulate\", \"from itertools import accumulate as _accumulate\")\n",
        "with open(\"dataset.py\", \"w\", encoding=\"utf-8\") as f:\n",
        "    f.write(code)\n",
        "\n",
        "# 3. The EXACT character string used by JaidedAI (including the degree symbol °)\n",
        "chars = \"0123456789!\\\"#$%&'()*+,-./:;<=>?@[\\\\]^_`{|}~ °ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz\"\n",
        "\n",
        "cmd = [\n",
        "    \"python\", \"train.py\",\n",
        "    \"--train_data\", \"dataset/train_lmdb\",\n",
        "    \"--valid_data\", \"dataset/val_lmdb\",\n",
        "    \"--select_data\", \"/\",\n",
        "    \"--batch_ratio\", \"1\",\n",
        "    \"--Transformation\", \"None\",\n",
        "    \"--FeatureExtraction\", \"VGG\",\n",
        "    \"--SequenceModeling\", \"BiLSTM\",\n",
        "    \"--Prediction\", \"CTC\",\n",
        "    \"--saved_model\", \"english_g2.pth\", # Use the fully pretrained model\n",
        "    \"--FT\",                            # Fine-tune mode (keeps prediction weights!)\n",
        "    \"--output_channel\", \"256\",         # Original model dimensions\n",
        "    \"--hidden_size\", \"256\",            # Original model dimensions\n",
        "    \"--batch_size\", \"128\",\n",
        "    \"--data_filtering_off\",\n",
        "    \"--workers\", \"4\",\n",
        "    \"--num_iter\", \"5000\",              # Only need 5000 steps since we're fine-tuning\n",
        "    \"--valInterval\", \"500\",\n",
        "    \"--exp_name\", \"apex_easyocr_finetune\",\n",
        "    \"--sensitive\",\n",
        "    \"--batch_max_length\", \"256\",\n",
        "    \"--character\", chars\n",
        "]\n",
        "\n",
        "print(\"Starting fine-tuning! Output will stream below...\")\n",
        "process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)\n",
        "for line in process.stdout:\n",
        "    sys.stdout.write(line)\n",
        "    sys.stdout.flush()\n",
        "process.wait()\n"
      ],
      "execution_count": None,
      "outputs": []
    },
    {
      "cell_type": "markdown",
      "metadata": {"id": "markdown-4"},
      "source": ["# 4. Download Trained Weights"]
    },
    {
      "cell_type": "code",
      "metadata": {"id": "code-4"},
      "source": [
        "from google.colab import files\n",
        "files.download('saved_models/apex_easyocr_finetune/best_accuracy.pth')"
      ],
      "execution_count": None,
      "outputs": []
    }
  ]
}

with open('EasyOCR_Apex_Finetune.ipynb', 'w', encoding='utf-8') as f:
    json.dump(notebook, f, indent=2)
