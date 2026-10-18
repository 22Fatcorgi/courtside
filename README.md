# COURTSIDE

繁體中文 NBA 球員數據、近 5 場焦點排行、賽事查詢與新聞。

網站：https://22fatcorgi.github.io/courtside/

## 安裝更新包

1. 先解壓縮更新包，保留資料夾結構。
2. 開啟 https://github.com/22Fatcorgi/courtside/upload/main 。
3. 把解壓縮後最外層的所有檔案與資料夾一起拖入上傳頁面，包含 `.github`、`scripts`、`tests`、`data`、`index.html`、`README.md`、`.gitignore`；不要上傳 ZIP 本身，也不要把整個外層包裝資料夾放進儲存庫。
4. 確認上傳路徑有 `.github/workflows/update-data.yml`、`scripts/update_data.py`、`data/latest.json` 與最外層的 `index.html`，提交到 `main`。
5. 到 **Actions → Update NBA data** 查看第一次執行結果。若沒有開始，可按 **Run workflow** 手動啟動。
6. 第一次雲端執行尚需驗證。綠勾且網站讀到新的取得時間，才表示背景更新成功啟用；紅叉時需查看來源錯誤，不能把本機測試成功當成雲端已成功。

原本的 Pages 設定維持 `Deploy from a branch → main → / (root)`，不必改成其他發布方式。

## 自動更新

`Update NBA data` GitHub Actions 工作流程每 4 小時檢查一次 NBA 官方資料，預定臺灣時間為 00:23、04:23、08:23、12:23、16:23、20:23；GitHub 排程可能延後。

更新項目：最新賽季各賽事類型的球員統計、完整逐場紀錄所計算的最近 5 場排行、目前球隊名單與 NBA 官方新聞。排程不需要有人開著網站。

成功結果儲存於 `data/latest.json`。網頁在開啟時及每小時從儲存庫的 raw URL 讀取這個檔案，沿用原有 GitHub Pages 發布設定。排程提交不需要再觸發 Pages 建置。網站上的「重新載入數據」會讀取最新已完成的背景結果，不會額外啟動 NBA 抓取。

ESPN／緯來新聞及比分維持在頁面開啟時連線，每小時檢查。歷史賽季保留原有資料；最新賽季有例行賽或季後賽資料時，未手動選期間的首頁會切換到最新有數據的賽事類型。

## 查看或手動執行

到儲存庫上方 **Actions → Update NBA data**，查看最近執行結果。若要立即執行，選 **Run workflow → main → Run workflow**。

綠勾代表來源全部完成；紅叉代表至少一個來源失敗。成功的其他項目仍會保存，失敗項目保留上次成功的數字與時間，並在網頁更新狀態列提示。來源資料不完整、球員數突然大量減少、出賽場數或比賽日期倒退時，也保留舊版。若來源格式改變或 GitHub 雲端無法連線，需修復來源後再確認成功，不會把舊資料標成剛更新。

## 統計定義

- 賽季數據：該賽季、該賽事類型的 NBA 球員實際出賽紀錄；不混入國際訪問球隊。
- 焦點球員：每位球員最近 5 場實際出賽的平均得分前三名，出賽滿 5 場才列入。同分按英文姓名排序，命中率以總進球除以總出手計算。卡片可查看 5 場排名依據。
- 現屬球隊：NBA 目前球員名單，與歷史統計球隊分開。
- TOT：統計期間確定曾代表多支 NBA 球隊的合計；不是一支球隊。
- 球隊未提供：來源或目前名單無法確認球隊，不會用 TOT 代替。
- 收藏：保存在目前瀏覽器，不提供帳號或跨裝置同步。

## 維護

修改網站介面時更新根目錄 `index.html`。修改抓取流程使用 `scripts/update_data.py`，修改頻率使用 `.github/workflows/update-data.yml`。若複製到另一個儲存庫，需要一起修改 HTML 中的 `LIVE_DATA_URL`。

本機驗證：

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
python scripts/update_data.py
```

Python 僅使用標準函式庫，不需 API 金鑰。球員照片連至 NBA CDN，新聞卡片連回原始報導。
