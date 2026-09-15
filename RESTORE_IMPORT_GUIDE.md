# Restore / Import Guide — สมบัติสี่จักรพรรดิ

## วิธี Export
- เข้าหน้าแอดมิน
- กดปุ่ม `Export ข้อมูล`
- ระบบจะส่งข้อมูล JSON สำหรับ users / rooms / periods

## วิธี Restore แบบง่าย
1. สำรองไฟล์ฐานข้อมูล `loyalty.db` ก่อนเสมอ
2. นำ JSON ที่ export ไปใช้กับสคริปต์ import ภายใน (ยังไม่มี UI อัตโนมัติ)
3. หากต้องการ restore เต็มระบบ ให้ใช้การกู้ไฟล์ `loyalty.db` มากกว่า JSON export

## หมายเหตุ
- ตอนนี้ export เน้นการสำรองข้อมูลเชิงอ้างอิง/ย้ายค่าพื้นฐาน
- สำหรับ production จริง แนะนำใช้ full DB backup เป็นหลัก
