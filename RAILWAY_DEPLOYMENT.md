# Railway Deployment Guide

## 1) เตรียม environment variables ใน Railway
ตั้งค่าตัวแปรต่อไปนี้ใน Railway → Variables:

- `SECRET_KEY`: ใช้ค่า random ที่ปลอดภัย เช่น `openssl rand -hex 32`
- `DATABASE_URL`: ใช้ค่าจาก Railway Postgres plugin หรือฐานข้อมูล PostgreSQL ที่มีอยู่
- `SQLITE_FILENAME`: ถ้าใช้ SQLite ให้ใช้ `/data/loyalty.db` และต้องสร้าง Railway Volume ที่ mount `/data`
- `UPLOAD_FOLDER`: ใช้ `./static/uploads`

> แอปนี้รองรับ `DATABASE_URL` ที่เริ่มด้วย `postgres://` และจะแปลงให้ใช้ `postgresql+psycopg://` โดยอัตโนมัติ

## 2) ตั้งค่าการเก็บข้อมูลถาวร
เลือกอย่างใดอย่างหนึ่งก่อน deploy:

- แนะนำ: เพิ่ม Railway Postgres แล้วตั้ง `DATABASE_URL`
- SQLite: สร้าง Volume ให้ service เดิมและ mount ที่ `/data` เพื่อให้ไฟล์ `/data/loyalty.db` อยู่ข้ามการ deploy

ห้ามใช้ SQLite ในโฟลเดอร์โปรเจกต์บน Railway เพราะไฟล์อาจหายเมื่อมีการสร้าง release ใหม่

## 3) Deploy จาก GitHub
1. นำ repository นี้ไปเชื่อมกับ Railway
2. เลือก Project → Deploy
3. Railway จะใช้ `Procfile` หรือ `railway.json` เพื่อรันแอป

## 4) ตรวจสอบการทำงาน
หลัง deploy สำเร็จ ให้เปิดหน้า:

- `/`
- `/results`
- `/lottery/rooms`
- `/wallet`
- `/bank-accounts`

และทดสอบ login/register, admin, partner, wallet, withdrawal request

## 5) ข้อแนะนำ
- ใช้ Railway Postgres plugin เพื่อให้ข้อมูลคงอยู่
- ตั้ง `SECRET_KEY` ให้เป็นค่า unique ต่อ environment
- รัน `pytest -q` ก่อน deploy เพื่อเช็กความสมบูรณ์
