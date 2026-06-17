import re

# Read the file
with open('asf_io.py', 'r', encoding='utf-8') as f:
    content = f.read()

# The buggy function to replace (lines 255-287)
buggy_function = r'''def read_asf_rtc_image_data\(urls: list, max_workers: int = 2, retry_timeout_seconds: float = 600\.0\):
    """
    Download and read GeoTIFFs in parallel, preserving strict order of input URLs\.

    Returns:
        arrs: list of arrays \(None on failure\)
        profs: list of profiles \(None on failure\)
        error_types: list per scene: None=success, 'not_found', 'network_error'
    """
    N = len\(urls\)
    results = \[\(None, None, None\)\] \* N

    with concurrent\.futures\.ThreadPoolExecutor\(max_workers=max_workers\) as ex:
        fut2idx = \{ex\.submit\(read_one_asf, url, retry_timeout_seconds\): i for i, url in enumerate\(urls\)\}
        for fut in tqdm\(concurrent\.futures\.as_completed\(fut2idx\), total=N, desc="Downloading"\):
            i = fut2idx\[fut\]
            try:
                results\[i\] = fut\.result\(\)
            except Exception:
                results\[i\] = \(None, None, "network_error"\)

    arrs = \[r\[0\] for r in results\]
    profs = \[r\[1\] for r in results\]
    error_types = \[r\[2\] for r in results\]

    success_count = sum\(1 for a in arrs if a is not None\)
    not_found_count = sum\(1 for e in error_types if e == "not_found"\)
    network_fail_count = sum\(1 for e in error_types if e == "network_error"\)
    logging\.info\(
        "Download complete: %d/%d success, %d not_found \(404\), %d network errors",
        success_count, N, not_found_count, network_fail_count,
    \)
    return arrs, profs, error_types'''

# The fixed function
fixed_function = '''def read_asf_rtc_image_data(urls: list, max_workers: int = 2, retry_timeout_seconds: float = 600.0):
    """
    Download and read GeoTIFFs in parallel, preserving strict order of input URLs.

    Returns:
        arrs: list of arrays (None on failure)
        profs: list of profiles (None on failure)
        error_types: list per scene: None=success, 'not_found', 'network_error'
    """
    N = len(urls)
    results = [(None, None, None)] * N

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        fut2idx = {ex.submit(read_one_asf, url, retry_timeout_seconds): i for i, url in enumerate(urls)}
        
        # Use wait() with explicit timeout instead of tqdm loop to avoid deadlock on last future
        timeout_budget = retry_timeout_seconds * 2 + 30  # Add buffer for final processing
        completed, not_done = concurrent.futures.wait(
            fut2idx.keys(),
            timeout=timeout_budget,
            return_when=concurrent.futures.ALL_COMPLETED
        )
        
        # Process completed futures with progress tracking
        completed_count = 0
        for fut in completed:
            i = fut2idx[fut]
            try:
                results[i] = fut.result()
            except Exception:
                results[i] = (None, None, "network_error")
            completed_count += 1
            logging.debug("Downloaded %d/%d scenes", completed_count, N)
        
        # Handle any futures that timed out (should be rare)
        if not_done:
            logging.error("Timeout: %d futures not completed after %.0f seconds", len(not_done), timeout_budget)
            for fut in not_done:
                i = fut2idx[fut]
                results[i] = (None, None, "network_error")

    arrs = [r[0] for r in results]
    profs = [r[1] for r in results]
    error_types = [r[2] for r in results]

    success_count = sum(1 for a in arrs if a is not None)
    not_found_count = sum(1 for e in error_types if e == "not_found")
    network_fail_count = sum(1 for e in error_types if e == "network_error")
    logging.info(
        "Download complete: %d/%d success, %d not_found (404), %d network errors",
        success_count, N, not_found_count, network_fail_count,
    )
    return arrs, profs, error_types'''

# Replace
new_content = re.sub(buggy_function, fixed_function, content, flags=re.MULTILINE)

# Verify replacement happened
if new_content != content:
    with open('asf_io.py', 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("SUCCESS: Function replaced")
else:
    print("FAILED: Pattern not found")
