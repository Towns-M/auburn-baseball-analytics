import sys, os, subprocess, logging, ftplib, io

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

sys.path.insert(0, os.path.expanduser('~/.local/lib/python3.12/site-packages'))

from azure.storage.blob import BlobServiceClient

FTP_HOST = 'ftp.trackmanbaseball.com'
FTP_USER = 'Auburn'
FTP_PASS = 'kA#R2,KNAP'
FTP_ROOT = '/v3'
CONTAINER = 'raw-stats'

def get_conn():
    r = subprocess.run(
        ['az','storage','account','show-connection-string',
         '-n','baseballstatsstore','-g','baseball-stats-rg',
         '--query','connectionString','-o','tsv'],
        capture_output=True, text=True)
    conn = r.stdout.strip()
    if not conn:
        raise RuntimeError("Could not get connection string")
    return conn

def get_existing_blobs(client):
    container = client.get_container_client(CONTAINER)
    logging.info("Listing existing blobs in raw-stats...")
    existing = set()
    for b in container.list_blobs():
        existing.add(b.name)
    logging.info(f"Found {len(existing)} existing blobs")
    return existing

def walk_ftp(ftp, path, files):
    try:
        entries = []
        ftp.retrlines('LIST ' + path, entries.append)
        for e in entries:
            parts = e.split(None, 8)
            if len(parts) < 9: continue
            name = parts[8]
            if name in ('.', '..'): continue
            full = path.rstrip('/') + '/' + name
            if parts[0].startswith('d'):
                walk_ftp(ftp, full, files)
            elif name.lower().endswith('.csv'):
                files.append(full)
    except Exception as ex:
        logging.warning(f"FTP walk error at {path}: {ex}")

def ingest():
    conn = get_conn()
    blob_client = BlobServiceClient.from_connection_string(conn)
    existing = get_existing_blobs(blob_client)
    container_client = blob_client.get_container_client(CONTAINER)

    logging.info("Connecting to FTP...")
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, 21, timeout=30)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)
    logging.info("FTP connected. Walking /v3 for CSV files...")

    all_ftp_files = []
    walk_ftp(ftp, FTP_ROOT, all_ftp_files)
    logging.info(f"Total FTP CSV files found: {len(all_ftp_files)}")

    to_upload = []
    for ftp_path in all_ftp_files:
        blob_name = ftp_path[len(FTP_ROOT)+1:]  # strips /v3/ -> 2024/08/12/CSV/file.csv
        if blob_name not in existing:
            to_upload.append((ftp_path, blob_name))

    logging.info(f"New files to upload: {len(to_upload)}")

    uploaded = 0
    errors = 0
    for ftp_path, blob_name in to_upload:
        try:
            buf = io.BytesIO()
            ftp.retrbinary('RETR ' + ftp_path, buf.write)
            buf.seek(0)
            container_client.upload_blob(name=blob_name, data=buf, overwrite=False)
            uploaded += 1
            if uploaded % 500 == 0:
                logging.info(f"Uploaded {uploaded}/{len(to_upload)}...")
        except Exception as ex:
            logging.warning(f"Failed {ftp_path}: {ex}")
            errors += 1

    ftp.quit()
    logging.info(f"DONE. Uploaded: {uploaded}, Errors: {errors}")

if __name__ == '__main__':
    ingest()
