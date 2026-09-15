# Deployment Checklist — สมบัติสี่จักรพรรดิ

## 1) ก่อนขึ้น Production
- ตั้งค่า `SECRET_KEY` ผ่าน environment variable
- ใช้ฐานข้อมูลจริง (ไม่ใช้ debug sqlite ชั่วคราวถ้าเป็น production จริง)
- ปิด debug mode ใน production
- ตรวจสอบ `requirements.txt` ติดตั้งได้สะอาด
- รัน `pytest -q` ให้ผ่านก่อน deploy

## 2) ความปลอดภัย
- ตรวจสอบ redirect `next` ที่ login ว่าอนุญาตเฉพาะ path ภายใน
- จำกัดสิทธิ์ route หลังบ้านด้วย `admin_required`
- ใช้ HTTPS เท่านั้นสำหรับลิงก์ภายนอก/ช่องทางติดต่อ
- สำรองฐานข้อมูลก่อนอัปเดต schema

## 3) ข้อมูลเริ่มต้น
- ตรวจสอบ seed ห้องหวยและหมวดหวย
- ตรวจสอบ VIP tier เริ่มต้น
- ตรวจสอบอัตราจ่ายเริ่มต้น
- ตั้งค่าลิงก์ติดต่อแอดมินในหลังบ้าน
- ตั้งค่าแบรนด์เว็บ (ชื่อเว็บ/คำโปรย/ไอคอน) จากหลังบ้าน

## 4) หลัง deploy
- เปิดหน้า `/`, `/results`, `/lottery/rooms`, `/wallet`, `/bank-accounts`
- ทดสอบ login/register
- ทดสอบผูกธนาคารและส่งคำขอถอน
- ทดสอบสิทธิ์ admin/member/partner
