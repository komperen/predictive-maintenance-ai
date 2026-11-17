# Panduan: GitHub -> GHCR -> PCAI (langkah demi langkah)

Ganti `<USER>` dengan username GitHub kamu (huruf kecil untuk nama image).

## 1. Buat repository di GitHub
1. Login ke github.com -> tombol **+** -> **New repository**.
2. Nama: `predictive-maintenance-ai`. Pilih Public (paling mudah). **Jangan** centang Add README / .gitignore (sudah ada di project).
3. Klik **Create repository**.

## 2. Buat Personal Access Token (untuk git push)
GitHub tidak menerima password biasa. Settings -> Developer settings -> Personal access tokens -> **Tokens (classic)** -> Generate new token, centang scope **repo** dan **workflow**, lalu salin tokennya. Token ini dipakai sebagai "password" saat `git push`. Jangan dibagikan.
(Alternatif: install GitHub CLI lalu `gh auth login`.)

## 3. Push project
```bash
unzip predictive-maintenance-ai-repo.zip
cd predictive-maintenance-ai
git init -b main
git add .
git status          # pastikan TIDAK ada .env atau file token
git commit -m "Predictive Maintenance AI v4.0.0"
git remote add origin https://github.com/<USER>/predictive-maintenance-ai.git
git push -u origin main      # username = <USER>, password = Personal Access Token
```

## 4. Tunggu GitHub Actions
Tab **Actions** -> workflow **Build image and Helm chart**. Setelah centang hijau (3-6 menit), image ada di `ghcr.io/<USER>/predictive-maintenance-ai:4.0.0`. Jika merah, buka log langkah yang gagal dan kirim ke saya. Hasil `helm lint` juga ada di sini.

## 5. Jadikan package Public
Profil GitHub -> **Packages** -> `predictive-maintenance-ai` -> **Package settings** -> **Change visibility** -> Public. (Kalau tetap Private, buat `imagePullSecret` di PCAI.)

## 6. Sesuaikan owner image
Di file `values.yaml` chart, `image.repository` default-nya `ghcr.io/komperen/predictive-maintenance-ai`. Ganti ke `ghcr.io/<USER>/predictive-maintenance-ai` (bisa juga diubah saat langkah Import di PCAI).

## 7. Import ke PCAI
1. Buka PCAI -> **Tools & Frameworks** -> **Import Framework**.
2. Isi Name `Predictive Maintenance AI`, kategori Data Science (atau yang tersedia), upload `predictive-maintenance-ai-0.1.0.tgz`.
3. Next -> review -> **Import**. Lalu di kartu aplikasi klik **Deploy / Install**.
4. Buka `https://pm-ai.<domain-PCAI-kamu>`.

## 8. Pasang token model Qwen (jangan di GitHub)
**Cara A - paling cepat untuk demo:** buka aplikasi -> **Settings & Demo** -> AI provider **HPE PCAI** -> tempel token pada *Access token* -> **Save** -> **Test connection**. Endpoint dan model sudah terisi.

**Cara B - lewat Kubernetes Secret (tahan restart):**
```bash
kubectl get ns | grep -i pm        # cari namespace aplikasi
kubectl -n <namespace> create secret generic pm-ai-secrets --from-literal=LLM_API_KEY='<TOKEN MLIS>'
```
Lalu set `llm.existingSecret: pm-ai-secrets` pada values dan re-deploy.

## 9. Troubleshooting
- `ImagePullBackOff` -> package masih Private atau `image.repository` salah.
- Test connection: *TLS certificate not trusted* -> hilangkan centang Verify TLS (lab).
- Test connection: *could not resolve host* -> tambahkan `hostAliases` (IP gateway PCAI) di values.
- *401/403* -> token salah atau kedaluwarsa; ambil token baru dari MLIS.

## 10. Keamanan
Token MLIS yang pernah ditempel di chat harus dianggap bocor: **rotasi/regenerate setelah demo**. Jangan masukkan token ke repo, values.yaml, atau zip.
