# Semantic Chunk Quality Analysis

**Total chunks analyzed:** 563

## Size Metrics

| Metric | Value |
|--------|-------|
| Average length | 634.4 chars |
| Median length | 558 chars |
| Very short (<50) | 19 (3.4%) |
| Very long (>2000) | 0 |

## Quality Issues

| Issue | Count | Rate |
|-------|-------|------|
| Fragmentation | 11 | 2.0% |
| Malformed OCR | 276 | 49.0% |
| Incomplete sentences | 389 | 69.1% |
| Exact duplicates | 3 | |
| Near-duplicates (sample) | 3 | |
| Section metadata present | 41 | |

## Length Distribution

- **0-49:** 19
- **150-399:** 145
- **400-799:** 162
- **50-149:** 59
- **800+:** 178

## Poor Retrieval Units (sample)

- `semantic_021_01` p.21 — ['malformed_ocr', 'incomplete_sentence']: "در این فصل حدود مجاز مواجهه شغلی عوامل زیان‌آور شیمیایی به همراه مطالب تکمیلی مفید

جهت بیان بهتر واژه های اختصاصی و تعا..."
- `semantic_021_02` p.21 — ['malformed_ocr', 'incomplete_sentence']: "بیماری های قبلی شوند. در این،موارد متخصصین طب کار بایستی این گروه از افراد را شناسایی و تحت

مراقبت ویژه قرار دهند. بناب..."
- `semantic_023_01` p.23 — ['malformed_ocr', 'incomplete_sentence']: "حدود مجاز مواجهه شغلی1 (OELs) به غلظت آلاینده های هوابرد مواد شیمیایی اشاره دارد و

شرایطی را بیان می کند که اگر کارگران..."
- `semantic_024_01` p.24 — ['malformed_ocr', 'incomplete_sentence']: "حساسیت بیش از حدبهیک مادۀ شیمیایی ممکن است در اثر عوامل مختلفی مانند: سن، جنسیت

خصوصیات ژنتیکی (استعداد ابتال بهیک بیما..."
- `semantic_025_02` p.25 — ['malformed_ocr', 'incomplete_sentence']: "دوره15 دقیقه ایازیک نوبت ی کار ید نبا غلظت آن ماده ازاینحدب یشتر باشد. حتی اگر میانگین

شیمیایی است که کارگران می توانند..."
- `semantic_026_01` p.26 — ['malformed_ocr', 'incomplete_sentence']: "4) رخوت و خواب آلودگی به حدی که احتمال ا یجاد آسیب بر اثر حادثه را افزایش دهد یا توانایی

فرد را برا ی دور شدن از عامل ح..."
- `semantic_027_01` p.27 — ['malformed_ocr', 'incomplete_sentence']: "تحریک فیزیکی ممکن است شروع کننده، افزایش دهنده یا تسریع کنند ه اثرات بهداشتی یان زآوراز

یقطر برهمکنش باسایر عوامل شیمیا..."
- `semantic_029_01` p.29 — ['malformed_ocr', 'incomplete_sentence']: "(همیشه کوچکتر از میانگین حسابی و مقداری است که بستگی به انحراف معیار هندسیsdg) دارد. در

توزیع لگ نرمال، انحراف معیار ه...."
- `semantic_029_02` p.29 — ['malformed_ocr', 'incomplete_sentence']: "نظر گرفته شود. چنانچه در برخی از محیط های متداول کاری، انحراف معیار هندسی بیشتر از عدد2 و

توزیعداده ها مشخص باشد و چنان..."
- `semantic_032_01` p.32 — ['malformed_ocr', 'incomplete_sentence']: "تغییرات در شرایط و برنامه‌های کاری

کاربرد حدود مجاز مواجهه برای شرا یطمح یطی غ یرمعمول

زمانی که شرایط دما و فشار محیط ..."
- `semantic_033_01` p.33 — ['malformed_ocr', 'incomplete_sentence']: "برنامه های کاری غیرمعمول برای مشاغل با گردش کار هفتگی

کاربرد حدود مجاز مواجهه برای مشاغلی که برنامه های (زمان ی بند) کا..."
- `semantic_033_02` p.33 — ['malformed_ocr', 'incomplete_sentence']: "اول، کارگر با مقداری معادل8 برابرOEL-TWA تماس داشته باشد ودر مابقی زمان نوبت کار ی هیچ

مواجهه ای نداشته باشد). در این ش..."
- `semantic_034_05` p.34 — ['malformed_ocr', 'incomplete_sentence']: "حدود مجاز آئروسلها ًمعموال برحسب مقدار جرم مواد شیمیایی در(حجم هوا3 mg/m) بیان می‌شوند..."
- `semantic_035_01` p.35 — ['malformed_ocr', 'incomplete_sentence']: "حدود مجاز گازها و بخارات معموال برحسب قسمت در میلیون حجمی 1) mpp (آالینده در هوا یا

ممکن است برحسب یلی م گرم در مترمکعب..."
- `semantic_042_01` p.42 — ['malformed_ocr', 'incomplete_sentence']: "عالئم و حروف مخفف

‡:کاندید تغییر حد مجاز

A: سرطان ییزا)(ضمیمه الف

ALARA: میزان مواجهه در حداقل ممکن حفظ شود

C: حد مج..."

## Recommendations

1. Do NOT mutate immutable Gold — use versioned chunk transformation if merging fragments.
2. Filter very short fragments (<50 chars) at retrieval time unless query is exact match.
3. Consider sentence-boundary chunking for future corpus versions.
4. OCR cleanup pass as separate `semantic_text_v2` source_type (not overwriting v1).
