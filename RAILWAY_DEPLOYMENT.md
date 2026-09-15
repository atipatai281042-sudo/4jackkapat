# Railway Deployment Guide

## 1) เตรียม environment variables ใน Railway
ตั้งค่าตัวแปรต่อไปนี้ใน Railway → Variables:

- `SECRET_KEY`: ใช้ค่า random ที่ปลอดภัย เช่น `openssl rand -hex 32`
- `DATABASE_URL`: ใช้ค่าจาก Railway Postgres plugin หรือฐานข้อมูล PostgreSQL ที่มีอยู่
- `SQLITE_FILENAME`: โดยปกติให้ปล่อยว่างหรือใช้ `loyalty.db`
- `UPLOAD_FOLDER`: ใช้ `./static/uploads`

> แอปนี้รองรับ `DATABASE_URL` ที่เริ่มด้วย `postgres://` และจะแปลงให้ใช้ `postgresql+psycopg://` โดยอัตโนมัติ

## 2) Deploy จาก GitHub
1. นำ repository นี้ไปเชื่อมกับ Railway
2. เลือก Project → Deploy
3. Railway จะใช้ `Procfile` หรือ `railway.json` เพื่อรันแอป

## 3) ตรวจสอบการทำงาน
หลัง deploy สำเร็จ ให้เปิดหน้า:

- `/`
- `/results`
- `/lottery/rooms`
- `/wallet`
- `/bank-accounts`

และทดสอบ login/register, admin, partner, wallet, withdrawal request

## 4) ข้อแนะนำ
- ใช้ Railway Postgres plugin เพื่อให้ข้อมูลคงอยู่
- ตั้ง `SECRET_KEY` ให้เป็นค่า unique ต่อ environment
- รัน `pytest -q` ก่อน deploy เพื่อเช็กความสมบูรณ์
