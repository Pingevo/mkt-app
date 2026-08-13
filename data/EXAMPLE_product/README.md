# data/EXAMPLE_product/

โฟลเดอร์ตัวอย่าง — แสดงโครงสร้างที่ระบบคาดหวังสำหรับข้อมูลสินค้าดิบ

## โครงสร้างที่ระบบใช้

```
data/{product_id}/
├── {product_id}-spec.xlsx          # ไฟล์ spec สินค้า (Excel/PDF/CSV/...)
├── {product_id}-brochure.pdf       # หนังสือเล่ม/โบรชัวร์
├── {product_id}-images/            # (ถ้ามี) โฟลเดอร์ภาพประกอบ
└── ...                              # ไฟล์อื่นๆ ที่ต้องการ ingest
```

## วิธีใช้งานจริง

1. สร้างโฟลเดอร์ `data/{ชื่อสินค้า}/` (เช่น `data/MyProduct/`)
2. วางไฟล์ spec สินค้า (xlsx, pdf, csv, txt, docx) ลงไป
3. ระบบจะ ingest อัตโนมัติและสร้าง `cache/{ชื่อสินค้า}/product.json`

## ประเภทไฟล์ที่รองรับ

- **Text**: `.xlsx`, `.pdf`, `.csv`, `.txt`, `.docx`
- **Image**: `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp` (ส่งให้ vision model อธิบาย)
- **Video**: `.mp4`, `.webm`, `.mov` (สกัด transcript)
- **Audio**: `.mp3`, `.wav`, `.m4a` (สกัด transcript)

## หมายเหตุ

- โฟลเดอร์นี้เป็นตัวอย่างเท่านั้น — ลบทิ้งได้ถ้าไม่ต้องการ
- ข้อมูลจริงของลูกค้าถูก ignore ใน `.gitignore` (เช่น `data/Lagenio*/`)
