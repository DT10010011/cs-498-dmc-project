# CS 498 DMC – Stage 3 prototype (MongoDB + Flask)

A website for our Eurovision 2018 tweets prototype. It has an overview page (data model, a sample document, collection and index sizes) and one page per query (Q1–Q6). Each query page shows the inputs, the MongoDB pipeline, the results, and the `explain()` execution plan.

The data was loaded into MongoDB Atlas by the first `loader.py`, which uses the flat format: `user_id`, `screen_name`, `name`, `user_verified`, `country_code`, plus partial and compound indexes. About 1.2M tweets from files 3–10.

## Files
| File | What it is |
|---|---|
| `app.py` | Flask routes, result cache |
| `queries.py` | The six queries (pipelines + Python post-processing) |
| `db.py` | Atlas connection, `allowDiskUse` fallback for free tiers, explain summary |
| `templates/`, `static/` | HTML pages and CSS |
| `deploy/cs498-webapp.service` | systemd service for the VM |

## Run it locally (quick test)
First, in Atlas, go to **Security → Network Access → Add IP Address → Add Current IP Address**. Atlas only accepts connections from IPs on its allow list.

Open a terminal in the `stage3-webapp` folder (the one with `app.py`), then follow the steps for your OS.

### Windows (PowerShell)
Run one line at a time. Windows PowerShell doesn't support `&&`.
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env                # paste the Atlas connection string after MONGO_URI= and save
python app.py               # open http://localhost:8000, stop with Ctrl+C
```
- If `python` opens the Microsoft Store or isn't found, use `py` instead (`py -m venv venv`, `py app.py`).
- If activation fails with "running scripts is disabled on this system", run this once, then activate again:
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
- In Command Prompt (cmd) instead of PowerShell, activate with `venv\Scripts\activate.bat`.
- Next time, you only need `.\venv\Scripts\Activate.ps1` and then `python app.py`.

### Mac / Linux
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env                   # paste the Atlas connection string after MONGO_URI= and save
python3 app.py              # open http://localhost:8000, stop with Ctrl+C
```
- Next time, you only need `source venv/bin/activate` and then `python3 app.py`.

### Troubleshooting
- **ServerSelectionTimeoutError** on the overview page: your IP isn't on the Atlas allow list, or `MONGO_URI` is wrong.
- **KeyError: 'MONGO_URI'**: `.env` is missing or isn't in the same folder as `app.py`.

## Deploy on a GCP VM
Run steps 1–2 from your laptop (gcloud) or do them in the Cloud Console. The gcloud commands are the same on Windows, except that in PowerShell you replace the trailing `\` line continuations with a backtick `` ` ``, or put each command on one line. Everything from step 4's `gcloud compute ssh` onward runs on the VM, which is Linux, so use those commands as written.

1. **Create a small VM** (e2-small, Debian 12) or reuse one, and give it a network tag:
   ```bash
   gcloud compute instances create cs498-webapp --zone=us-central1-a \
     --machine-type=e2-small --image-family=debian-12 --image-project=debian-cloud --tags=flask
   ```
2. **Open port 8000** for the site:
   ```bash
   gcloud compute firewall-rules create allow-flask-8000 --allow=tcp:8000 --target-tags=flask
   ```
   The VM's external IP changes when it is stopped. Promote it to a static IP (VPC network → IP addresses → Reserve) so the URL and the Atlas allow-list entry stay valid.
3. **Allow the VM in Atlas**: Security → Network Access → Add IP Address → the VM's external IP.
4. **Copy the code** to the VM and install it:
   ```bash
   gcloud compute scp --recurse stage3-webapp cs498-webapp:~ --zone=us-central1-a
   gcloud compute ssh cs498-webapp --zone=us-central1-a
   # on the VM:
   sudo apt update && sudo apt install -y python3-venv
   cd ~/stage3-webapp
   python3 -m venv venv && venv/bin/pip install -r requirements.txt
   cp .env.example .env && nano .env      # paste MONGO_URI
   venv/bin/python app.py                 # test: http://EXTERNAL_IP:8000 , then Ctrl+C
   ```
5. **Keep it running** with systemd. Replace `YOUR_VM_USERNAME` in the service file (`whoami` prints it):
   ```bash
   sed -i "s/YOUR_VM_USERNAME/$(whoami)/g" deploy/cs498-webapp.service
   sudo cp deploy/cs498-webapp.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now cs498-webapp
   sudo systemctl status cs498-webapp     # logs: journalctl -u cs498-webapp -f
   ```
   The site is now at `http://EXTERNAL_IP:8000`. After changing code, run `sudo systemctl restart cs498-webapp`.

## Notes for the presentation
- **Changes from the Stage 2 design:** the field names are flat instead of nested under `user`. The indexes are compound/partial (`screen_name_1_created_at_1`, `user_verified_1_user_id_1_type_1`, and partial `in_reply_to_status_id_1` / `country_code_1`). Missing values are left out instead of stored as `null`. The planned `users` collection was dropped after its `$out` step hit the free tier's sort memory limit.
- **Execution plans:** use the **Explain plan** button. Q3 and Q6 should show `PROJECTION_COVERED → IXSCAN` with 0 documents examined (covered queries). Q2 and Q5 should show an IXSCAN on a partial index. Q4 should show `COLLSCAN`.
- **Before demoing**, run each query once so results are cached and the demo is instant. Tick "Run again" to show a live run.
- **Free-tier limits:** M0 refuses `allowDiskUse`. `db.py` detects this and falls back, so each `$group` must fit in 100 MB. That's why Q3 groups only `user_id`, then looks up names afterwards.