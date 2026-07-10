# 📊 TesseractApexOCR Pipeline Health Report
**Generated At**: `2026-07-03 19:23:38`  
**Overall Pipeline Health**: **🟡 WARNING**

## ⚙️ 1. Configuration Check (🟢 OK)
- **OCR Mode**: `EasyOCR` (custom fine-tuned model active: `True`)
- **Local TrOCR Model Directory**: `models\trocr_apex` (Exists: `True`)
- **TrOCR Conf Threshold**: `0.3`
- **Online Gemini Queue**: `Enabled` (Agree Threshold: `0.85`)
- **Active Twitch Streamers**: `6`

## 🗄️ 2. Database & Capture Freshness (🟢 OK)
- **Total Database Events**: `10,730`
- **Total Kills Logs**: `5,845`
- **Null Attacker Rate**: `1.78%` | **Null Victim Rate**: `0.82%`
- **Online Gemini Validations**: `5` (Corrections Applied: `5`)
- **Last Recorded Event**: `2026-07-03 17:01:36`

## 🔍 3. Parser & Normalizer Integrity (🟢 OK)
- **Self-Kills Count**: `0` (Indicative of OCR split failures)
- **Leaked Legends/Common Words**: `1` events
- **Top Leaked Common Words**: `extended` (1x)

## 👁️ 4. OCR Accuracy Benchmark (🟢 OK)
- **Benchmark Crop Sample Size**: `94`
- **Average String Similarity**: `91.21%`
- **Character Error Rate (CER)**: `15.16%`
- **Word Error Rate (WER)**: `37.81%`
- **Exact Match Accuracy**: `21.28%`
- **OCR Engines Evaluated**: `{'easyocr': 94}`

### Top OCR Character Confusions:
| Substitution | Count |
| --- | --- |
| `'ä' -> 'a'` | 13 |
| `'8' -> 'B'` | 9 |
| `'y' -> 'v'` | 9 |
| `'a' -> 'e'` | 5 |
| `'O' -> 'D'` | 3 |

## 🏆 5. ELO & Session Grouping Health (🟡 WARNING)
> [!WARNING]
> ELO Alert: 1 mega-matches (100+ kills) detected. Gap threshold is likely too high.

- **Leaderboard Players**: `1,363` (`142` with 3+ matches)
- **ELO Rating Distribution**: Mean: `1000.7` | StdDev: `181.6` | Min: `100.0` | Max: `1594.0`
- **Total Grouped Matches**: `192`
- **Mega-Matches (Stitching Bugs)**: `1`
- **Near-Duplicate Player Profiles**: `0` pairs (Requires deduplication)
