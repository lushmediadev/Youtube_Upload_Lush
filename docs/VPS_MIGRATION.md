# Youtube Upload VPS migration

Script này dùng mô hình **cài mới code từ GitHub rồi migrate runtime data**. Nó không clone nguyên cả VPS và không ghi gì lên VPS nguồn.

## Phạm vi

Script `scripts/migrate_control_plane_vps.sh` migrate phần control-plane/web app:

- repo checkout `/opt/youtube-upload-lush`
- runtime `.env`
- SQLite/data trong `/opt/youtube-upload-lush-runtime/backend-data`
- systemd service `youtube-upload-web.service`

Script không tự bê toàn bộ BOT worker sang VPS khác. BOT worker là agent riêng; sau khi control-plane mới chạy, có thể thêm lại BOT từ UI hoặc bootstrap worker bằng pipeline hiện có.

## Chuẩn bị

Trên VPS mới, cần SSH được vào VPS nguồn. Nên dùng SSH key:

```bash
ssh-copy-id root@OLD_VPS_IP
```

Nếu domain đổi sang VPS mới, chuẩn bị DNS/Caddy sau khi app chạy ổn. Không đổi DNS production trước khi verify.

## Chạy migrate control-plane

```bash
git clone https://github.com/lushmediadev/Youtube_Upload_Lush.git /opt/youtube-upload-lush
cd /opt/youtube-upload-lush
SOURCE_HOST=OLD_VPS_IP bash scripts/migrate_control_plane_vps.sh
```

## Tùy biến thường dùng

```bash
SOURCE_HOST=OLD_VPS_IP \
SOURCE_USER=root \
SOURCE_RUNTIME_DIR=/opt/youtube-upload-lush-runtime \
APP_DIR=/opt/youtube-upload-lush \
RUNTIME_DIR=/opt/youtube-upload-lush-runtime \
SERVICE_PORT=8000 \
SYSTEMD_SERVICE_NAME=youtube-upload-web.service \
bash scripts/migrate_control_plane_vps.sh
```

Chỉ chuẩn bị code/data, chưa start service:

```bash
SOURCE_HOST=OLD_VPS_IP START_APP=0 bash scripts/migrate_control_plane_vps.sh
```

Không copy `.env`:

```bash
SOURCE_HOST=OLD_VPS_IP SKIP_ENV=1 bash scripts/migrate_control_plane_vps.sh
```

Không copy DB/data:

```bash
SOURCE_HOST=OLD_VPS_IP SKIP_BACKEND_DATA=1 bash scripts/migrate_control_plane_vps.sh
```

Copy thêm `worker-data` nếu VPS nguồn cũng đang chạy worker local và bạn thật sự muốn mang artifact đó sang:

```bash
SOURCE_HOST=OLD_VPS_IP SKIP_WORKER_DATA=0 bash scripts/migrate_control_plane_vps.sh
```

## Sau khi migrate

Kiểm tra service:

```bash
systemctl status youtube-upload-web --no-pager
curl -fsS http://127.0.0.1:8000/api/health
sqlite3 /opt/youtube-upload-lush-runtime/backend-data/app_state.db 'PRAGMA integrity_check;'
```

Nếu muốn map domain mới, trỏ Caddy/Nginx tới port `8000` của control-plane. Sau đó verify public URL trước khi đổi DNS production.

## BOT workers sau khi migrate

BOT upload/live không nên copy mù từ VPS cũ. Cách an toàn:

1. Migrate control-plane trước.
2. Verify user/channel/job data.
3. Thêm BOT mới từ UI `Danh sách BOT`, hoặc bootstrap lại worker bằng script hiện có.
4. Khi worker đã online trên control-plane mới, mới cấp user/live quota lại nếu cần.

Nếu worker cũ vẫn trỏ về control-plane mới qua domain và tự register, kiểm tra kỹ `WORKER_SHARED_SECRET`, `CONTROL_PLANE_URL`, workspace mode và manager assignment trước khi cho chạy job thật.
