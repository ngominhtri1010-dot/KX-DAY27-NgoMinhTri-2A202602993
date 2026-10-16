# Báo cáo chạy thử Day27 — HITL PR Review Agent

Chạy ngày 16/10/2026 (giờ Việt Nam). Kết quả dưới đây là từ PR thật,
OpenRouter thật và bình luận GitHub đã đọc lại để xác minh.

| PR | Confidence | Luồng kiểm tra | Kết quả | Audit |
|---|---|---|---|---|
| #1 | 64% | human_approval → Edit → commit | Thành công | 6 sự kiện |
| #2 | 35% → 35% | escalate → Q&A → synthesize → human_approval → Approve → commit | Thành công | 9 sự kiện |

- [Bình luận PR #1](https://github.com/VinUni-AI20k/PR-Demo/pull/1#issuecomment-6206669379) — thread `lab-demo-20261016-pr1`.
- [Bình luận PR #2](https://github.com/VinUni-AI20k/PR-Demo/pull/2#issuecomment-6206690053) — thread `lab-demo-20261016-pr2`.

## Kiểm tra chi tiết

- PR #1 dùng lại phân tích trong cache; review được sửa để giữ các nhận xét
  có căn cứ về migration, kiểm tra priority và tests trước khi đăng.
- PR #2 nhận diện SQL interpolation, MD5, token được suy ra cục bộ, token file
  không ép quyền truy cập và truyền bearer token qua HTTP.
- Q&A do trợ lý cung cấp phục vụ demo, dựa trên diff; không giả định chính sách
  tổ chức hay ý định tác giả đã được xác nhận. Các rủi ro vẫn chưa được sửa.
- Sau tổng hợp, PR #2 vẫn đạt 35% và cần bước duyệt cuối. Approve chỉ đồng ý
  đăng bình luận review, không phải duyệt merge hoặc triển khai code có lỗi.
- Phiên PR #1 được khôi phục qua Streamlit từ SQLite thành công.
- PR #2 được khôi phục và bấm Approve qua Streamlit AppTest với API thật.
- Việc khôi phục UI và bấm duyệt không gọi thêm LLM (đã chặn factory LLM
  trong kiểm tra để phát hiện cuộc gọi ngoài dự kiến).
- Mỗi phiên chỉ có một sự kiện commit; reviewer_id được ghi ở các bước phản hồi.
- Không có sự kiện risk_level=high đi theo decision=auto.
- Bình luận ở cả hai PR đã được đọc lại qua GitHub API và so khớp tác giả/nội dung.

## Số lượt LLM

Model: `nvidia/nemotron-3.5-super-vl:free`.

Lần chạy này gọi thêm 2 lượt: phân tích PR #2 và tổng hợp sau Q&A.
PR #1 dùng cache. Tổng cộng từ lần kiểm tra trước: 3 lượt thành công,
được lưu thành 3 kết quả trong `llm_cache.db`.

## Minh chứng và replay

- `pr1-result.json`, `pr2-result.json`: trạng thái cuối, audit và URL bình luận.
- `pr2-escalation.json`: phân tích ban đầu và câu hỏi escalation.
- `pr2-refined-approval.json`: review đã tổng hợp, đang chờ duyệt cuối.
- `response-pr*.json`: nội dung phản hồi đã dùng cho demo.
- `pr1-replay.txt`, `pr2-replay.txt`: timeline audit đầy đủ.
- `pr1-ui.txt`, `pr2-approval-ui.txt`, `pr2-ui.txt`: nội dung UI đọc qua AppTest;
  đây là bản ghi văn bản, không phải ảnh chụp màn hình.

```bash
uv run python -m audit.replay --thread lab-demo-20261016-pr1
uv run python -m audit.replay --thread lab-demo-20261016-pr2
uv run streamlit run app.py --server.port 8502
```

Có thể mở phiên đã hoàn tất trong sidebar Recent sessions để xem kết quả.
Không cần chạy review mới hoặc đăng lại bình luận để lấy minh chứng.

Bài dùng SQLite đúng scaffold README; nếu giảng viên yêu cầu PostgreSQL bắt buộc,
phần PostgreSQL vẫn cần bổ sung. File `.env` và API key không nằm trong minh chứng.
