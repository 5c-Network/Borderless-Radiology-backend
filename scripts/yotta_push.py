import sys
import os
import csv
import tempfile
import shutil
import json
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
import pydicom
import time
import subprocess
import requests
import constant
from faker import Faker

# Suppress SSL warnings from boto3/urllib3
warnings.filterwarnings('ignore', message='Unverified HTTPS request')
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

WORKERS = 10
_csv_lock = threading.Lock()
_sheet_lock = threading.Lock()
_patient_id_counter = 0
_patient_id_lock = threading.Lock()

# Yotta S3 config
YOTTA_ACCESS_KEY = 'nm1_5c_network_user'
YOTTA_SECRET_KEY = 'Sv+tGfp8HOffcTdHwwBrc1mpVGYuc+svMP8m5DNW'
YOTTA_BUCKET = '5cnetwork-newserver-dicom'
YOTTA_ENDPOINT = 'https://nm1ecs.yotta.com:9021'

CSV_INPUT = os.path.join(os.path.dirname(__file__), '..', 'STAR- DB_ groundtruths - all.csv')
CSV_SUCCESS = os.path.join(os.path.dirname(__file__), '..', 'success.csv')
CSV_FAILED = os.path.join(os.path.dirname(__file__), '..', 'failed.csv')
CSV_DUPLICATES = os.path.join(os.path.dirname(__file__), '..', 'duplicates.csv')

GOOGLE_SHEET_URL = 'https://script.google.com/macros/s/AKfycbxb8Arjjq7U0c4Gp02wsHyafVmAW9hxe0XmFSYaTN1Pz9xV37B20b362Ocu5WsRQfF88A/exec'

_sheet_pushed = set()


def get_unique_patient_id():
    """Generate a unique patient ID across all threads."""
    global _patient_id_counter
    with _patient_id_lock:
        _patient_id_counter += 1
        return f'{int(time.time())}{_patient_id_counter:04d}'


def post_to_sheet(data):
    """Post a success row to Google Sheets via Apps Script doGet. Skips duplicates."""
    study_iuid = data.get('old_study_iuid', '')
    with _sheet_lock:
        if study_iuid in _sheet_pushed:
            return
        _sheet_pushed.add(study_iuid)
    try:
        requests.get(GOOGLE_SHEET_URL, params=data, timeout=15)
    except Exception as e:
        print(f'  [Sheet] Failed to post: {e}')


def get_s3_client():
    return boto3.client(
        's3',
        endpoint_url=YOTTA_ENDPOINT,
        aws_access_key_id=YOTTA_ACCESS_KEY,
        aws_secret_access_key=YOTTA_SECRET_KEY,
        verify=False,
    )


def list_s3_objects(s3_client, s3_path):
    """List all object keys under an S3 prefix."""
    prefix = s3_path.rstrip('/') + '/'
    paginator = s3_client.get_paginator('list_objects_v2')
    keys = []
    for page in paginator.paginate(Bucket=YOTTA_BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            filename = os.path.basename(obj['Key'])
            if filename and not filename.startswith('.'):
                keys.append(obj['Key'])
    return keys


def download_dicom_from_s3(s3_client, s3_path, local_dir):
    """Download all DICOM files and verify count matches S3."""
    keys = list_s3_objects(s3_client, s3_path)
    if not keys:
        return [], 0

    downloaded_files = []
    for key in keys:
        filename = os.path.basename(key)
        local_path = os.path.join(local_dir, filename)
        s3_client.download_file(YOTTA_BUCKET, key, local_path)

        # Verify file was written and has content
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            downloaded_files.append(local_path)

    return downloaded_files, len(keys)


def anonymize_dicom_files(dicom_files):
    """Anonymize all patient/client level data. Returns (new_study_iuid, old_patient_id, new_patient_id)."""
    fake = Faker('en_IN')
    new_study_uid = pydicom.uid.generate_uid()
    new_patient_id = get_unique_patient_id()
    patient_name = fake.name()
    referring_physician = fake.name()
    old_patient_id = ''

    for dicom_file in dicom_files:
        ds = pydicom.dcmread(dicom_file)

        if not old_patient_id:
            old_patient_id = getattr(ds, 'PatientID', '')

        ds.SpecificCharacterSet = 'ISO_IR 192'

        # Patient data
        ds.PatientName = patient_name
        ds.PatientID = new_patient_id
        ds.PatientBirthDate = ''
        ds.PatientSex = ''
        ds.PatientAge = ''
        ds.PatientAddress = ''
        ds.PatientTelephoneNumbers = ''

        # Referring Physician — same for all files in a study
        ds.ReferringPhysicianName = referring_physician
        ds.ReferringPhysicianAddress = ''
        ds.ReferringPhysicianTelephoneNumbers = ''

        # Institution / Center details
        ds.InstitutionName = ''
        ds.InstitutionAddress = ''
        ds.InstitutionalDepartmentName = ''
        ds.StationName = ''

        # Other physicians
        ds.PerformingPhysicianName = ''
        ds.RequestingPhysician = ''
        ds.NameOfPhysiciansReadingStudy = ''
        ds.OperatorsName = ''

        # UIDs
        ds.StudyInstanceUID = new_study_uid
        ds.SOPInstanceUID = pydicom.uid.generate_uid()

        # Other identifiers
        ds.AccessionNumber = ''
        ds.OtherPatientIDs = ''

        ds.save_as(dicom_file)

    return new_study_uid, old_patient_id, new_patient_id


def push_dicom_files(dicom_files):
    """Push DICOM files and verify all were sent."""
    expected_count = len(dicom_files)
    command = [
        'dcmsend',
        '-aet', constant.CLIENT_AETITLE,
        '-aec', constant.SERVER_AETITLE,
        constant.SERVER_IP,
        str(constant.SERVER_PORT),
    ]
    result = subprocess.run(command + dicom_files, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'dcmsend failed (exit {result.returncode}): {result.stderr.strip()}')

    return expected_count


def read_input_csv():
    rows = []
    with open(CSV_INPUT, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


SUCCESS_FIELDS = ['study_id', 'old_study_iuid', 'new_study_iuid', 'old_patient_id', 'new_patient_id', 'path', 'modstudy']
FAILED_FIELDS = ['study_id', 'old_study_iuid', 'path', 'modstudy', 'reason']


def append_to_csv(filepath, row, fieldnames):
    with _csv_lock:
        file_exists = os.path.exists(filepath) and os.path.getsize(filepath) > 0
        with open(filepath, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)


def read_already_processed():
    processed = set()
    if os.path.exists(CSV_SUCCESS) and os.path.getsize(CSV_SUCCESS) > 0:
        with open(CSV_SUCCESS, 'r', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                processed.add(row.get('old_study_iuid', '').strip())
    return processed


def read_failed_csv():
    rows = []
    if os.path.exists(CSV_FAILED) and os.path.getsize(CSV_FAILED) > 0:
        with open(CSV_FAILED, 'r', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                rows.append(row)
    return rows


def process_study(row, idx, total):
    """Process a single study with integrity checks."""
    s3_client = get_s3_client()

    old_study_iuid = (row.get('study_iuid') or row.get('stuyd_iuid') or row.get('old_study_iuid') or '').strip()
    s3_path = row.get('path', '').strip()
    study_id = row.get('study_id', '').strip()
    modstudy = row.get('modstudy', '').strip()

    print(f'[{idx}/{total}] Processing study_id={study_id} | study_iuid={old_study_iuid}')

    if not old_study_iuid or not s3_path:
        return False, {
            'study_id': study_id, 'old_study_iuid': old_study_iuid,
            'path': s3_path, 'modstudy': modstudy,
            'reason': 'Missing study_iuid or path in CSV',
        }

    tmp_dir = tempfile.mkdtemp(prefix='yotta_dicom_')
    try:
        # Download and verify count
        dicom_files, s3_count = download_dicom_from_s3(s3_client, s3_path, tmp_dir)
        if not dicom_files:
            raise FileNotFoundError(f'No DICOM files found at s3://{YOTTA_BUCKET}/{s3_path}')

        if len(dicom_files) != s3_count:
            raise RuntimeError(f'Download incomplete: got {len(dicom_files)}/{s3_count} files')

        print(f'  [{idx}] Downloaded {len(dicom_files)} file(s) [verified]')

        # Anonymize
        new_study_iuid, old_patient_id, new_patient_id = anonymize_dicom_files(dicom_files)

        # Verify all anonymized files are valid DICOM
        for f in dicom_files:
            ds = pydicom.dcmread(f)
            if ds.StudyInstanceUID != new_study_iuid:
                raise RuntimeError(f'Anonymization failed: UID mismatch in {os.path.basename(f)}')

        print(f'  [{idx}] Anonymized [verified]')

        # Push and verify
        pushed_count = push_dicom_files(dicom_files)
        print(f'  [{idx}] Pushed {pushed_count} file(s) [OK]')

        return True, {
            'study_id': study_id, 'old_study_iuid': old_study_iuid,
            'new_study_iuid': new_study_iuid, 'old_patient_id': old_patient_id,
            'new_patient_id': new_patient_id, 'path': s3_path, 'modstudy': modstudy,
        }

    except Exception as e:
        reason = str(e)
        print(f'  [{idx}] FAILED: {reason}')
        return False, {
            'study_id': study_id, 'old_study_iuid': old_study_iuid,
            'path': s3_path, 'modstudy': modstudy, 'reason': reason,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_batch(pending, total_offset=0):
    success_count = 0
    fail_count = 0
    total = len(pending)

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {}
        for idx, row in enumerate(pending, 1):
            f = executor.submit(process_study, row, idx + total_offset, total + total_offset)
            futures[f] = row

        for future in as_completed(futures):
            ok, result = future.result()
            if ok:
                append_to_csv(CSV_SUCCESS, result, SUCCESS_FIELDS)
                post_to_sheet({**result, 'status': 'success'})
                success_count += 1
            else:
                append_to_csv(CSV_FAILED, result, FAILED_FIELDS)
                fail_count += 1

    return success_count, fail_count


def main():
    test_iuid = sys.argv[1] if len(sys.argv) > 1 else None
    max_retries = 3

    print(f'Using {WORKERS} parallel workers')
    print('=' * 60)
    print('PHASE 1: Processing all studies from input CSV')
    print('=' * 60)

    input_rows = read_input_csv()
    already_done = read_already_processed()

    if test_iuid:
        input_rows = [r for r in input_rows if (r.get('study_iuid') or r.get('stuyd_iuid') or '').strip() == test_iuid]
        if not input_rows:
            print(f'No row found for study_iuid: {test_iuid}')
            return
        print(f'Test mode: running only for study_iuid={test_iuid}')

    # Filter out duplicate study_iuids from input CSV
    seen_iuids = set()
    unique_rows = []
    duplicate_rows = []
    for r in input_rows:
        iuid = (r.get('study_iuid') or r.get('stuyd_iuid') or '').strip()
        if iuid in seen_iuids:
            duplicate_rows.append(r)
        else:
            seen_iuids.add(iuid)
            unique_rows.append(r)

    if duplicate_rows:
        dup_fields = list(duplicate_rows[0].keys())
        for dr in duplicate_rows:
            append_to_csv(CSV_DUPLICATES, dr, dup_fields)
        print(f'Found {len(duplicate_rows)} duplicate study_iuids in CSV -> duplicates.csv')

    # Skip already processed
    pending = [r for r in unique_rows if (r.get('study_iuid') or r.get('stuyd_iuid') or '').strip() not in already_done]
    skipped = len(unique_rows) - len(pending)
    if skipped:
        print(f'Skipping {skipped} already processed studies (found in success.csv)')

    # Clear failed.csv for fresh run
    if not test_iuid and os.path.exists(CSV_FAILED):
        os.remove(CSV_FAILED)

    print(f'Processing {len(pending)} studies with {WORKERS} workers...')
    s, f = run_batch(pending)
    success_count = len(already_done) + s
    fail_count = f

    print(f'\nPhase 1 done — Success: {success_count} | Failed: {fail_count}')

    if test_iuid:
        return

    # --- Phase 2: Retry failed studies ---
    for attempt in range(1, max_retries + 1):
        failed_rows = read_failed_csv()
        if not failed_rows:
            print('No failures to retry. All done!')
            break

        print(f'\n{"=" * 60}')
        print(f'RETRY {attempt}/{max_retries} — {len(failed_rows)} failed studies')
        print(f'{"=" * 60}')

        os.remove(CSV_FAILED)
        rs, rf = run_batch(failed_rows)

        print(f'\nRetry {attempt} done — Recovered: {rs} | Still failed: {rf}')
        if rf == 0:
            print('All retries succeeded!')
            break

    # --- Summary ---
    final_failed = read_failed_csv()
    final_success_count = len(read_already_processed())
    print(f'\n{"=" * 60}')
    print(f'MIGRATION COMPLETE')
    print(f'Total success: {final_success_count}')
    print(f'Total failed:  {len(final_failed)}')
    print(f'Success CSV:   {CSV_SUCCESS}')
    print(f'Failed CSV:    {CSV_FAILED}')
    print(f'{"=" * 60}')


if __name__ == '__main__':
    main()
