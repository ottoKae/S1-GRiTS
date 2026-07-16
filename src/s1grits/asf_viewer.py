import os
import requests
from PIL import Image
from io import BytesIO

import asf_search as asf
import defusedxml.ElementTree as ET
import pandas as pd


# ===============================================
# view browse file
# ===============================================

def get_rtc_browse(item, browse_type='standard', save_dir=None, verbose=True):
    """
    Download and save the OPERA RTC browse image at specified resolution.

    Args:
        item: A single product item from ASF search results (e.g., resp[0]).
        browse_type: One of 'standard', 'low-res', 'thumbnail'.
        save_dir: Directory to save the image, defaults to "../output".
        verbose: Whether to print status logs.

    Returns:
        A PIL.Image.Image object if successful; None if failed.
    """
    if save_dir is None:
        save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")
        save_dir = os.path.normpath(save_dir)
    browse_list = item.properties.get('browse', [])
    scene_name = item.properties.get('sceneName', 'unknown_scene')

    if not browse_list:
        print("[ERROR] No browse image links found.")
        return None

    # Map browse_type to filename keyword
    type_map = {
        'standard': lambda url: 'low-res' not in url and 'thumbnail' not in url,
        'low-res': lambda url: 'low-res' in url,
        'thumbnail': lambda url: 'thumbnail' in url
    }

    if browse_type not in type_map:
        print(f"[ERROR] Invalid browse_type: {browse_type}. Must be one of {list(type_map.keys())}")
        return None

    # Choose the first matching https URL
    url = next((url for url in browse_list if url.startswith('https') and type_map[browse_type](url)), None)
    if not url:
        print(f"[ERROR] No matching browse image of type '{browse_type}' found.")
        return None

    try:
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f"browser_{scene_name}_{browse_type}.png")

        headers = {"User-Agent": "Mozilla/5.0"}
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(max_retries=3)
        session.mount('https://', adapter)

        response = session.get(url, headers=headers, timeout=60)
        response.raise_for_status()

        img = Image.open(BytesIO(response.content))
        img.save(save_path)

        if verbose:
            print(f"[INFO] Successfully saved {browse_type} browse image:")
            print(f"       {save_path}")
            print(f"[INFO] Image size: {img.size}, Format: {img.format}")

        return img

    except Exception as e:
        print(f"[ERROR] Failed to download/save browse image: {e}")
        return None

# ===============================================
# view xml file
# ===============================================

def get_utm_zone(epsg):
    """
    Calculate the UTM Zone (e.g., '50N') based on the EPSG code.

    Args:
        epsg (str or int): The EPSG code (e.g., '32650').

    Returns:
        str: The UTM zone string (e.g., '50N', '50S') or 'Unknown'.
    """
    if not epsg or not str(epsg).isdigit():
        return None

    code = int(epsg)

    # Northern Hemisphere (32601 - 32660)
    if 32601 <= code <= 32660:
        return f"{code - 32600}N"

    # Southern Hemisphere (32701 - 32760)
    elif 32701 <= code <= 32760:
        return f"{code - 32700}S"

    return "Unknown"

def parse_opera_iso_xml(xml_url: str, verbose=True) -> pd.DataFrame:
    """
    Download and parse OPERA RTC ISO XML metadata.

    This function implements strict XPath handling to accurately extract
    metadata fields like Title, Platform, and Bounding Box, avoiding
    common pitfalls with ISO 19115 nested structures.

    Args:
        xml_url (str): The URL to the .iso.xml file.
        verbose (bool): Whether to print the extracted metadata.

    Returns:
        pd.DataFrame: A single-row DataFrame containing the metadata.
    """
    try:
        # 1. Fetch Content
        session = asf.ASFSession()
        response = session.get(xml_url, timeout=30)
        response.raise_for_status()
        root = ET.fromstring(response.content)

        # 2. Define Namespaces (Added 'eos')
        ns = {
            'gmd': 'http://www.isotc211.org/2005/gmd',
            'gco': 'http://www.isotc211.org/2005/gco',
            'gml': 'http://www.opengis.net/gml/3.2',
            'gmi': 'http://www.isotc211.org/2005/gmi',
            'gmx': 'http://www.isotc211.org/2005/gmx',
            'eos': 'http://earthdata.nasa.gov/schema/eos' # New Namespace
        }

        # 3. Basic Helper Function
        def find_value(paths, use_gco=True, root_elem=None):
            if root_elem is None: root_elem = root
            if isinstance(paths, str): paths = [paths]
            for path in paths:
                elem = root_elem.find(path, ns)
                if elem is not None:
                    if use_gco:
                        gco = elem.find('gco:CharacterString', ns)
                        if gco is not None and gco.text: return gco.text
                        anchor = elem.find('gmx:Anchor', ns)
                        if anchor is not None and anchor.text: return anchor.text
                    else:
                        if elem.text: return elem.text
            return None

        # 4. New Helper: Extract from eos:AdditionalAttribute list
        def get_eos_attribute(target_name):
            """
            Search through all eos:AdditionalAttribute elements to find
            the one where eos:name matches 'target_name', then return its value.
            """
            # Find all AdditionalAttribute nodes
            # Usually located under gmi:acquisitionInformation or directly under root depending on structure
            # We use .// to search deeply
            all_attrs = root.findall('.//eos:AdditionalAttribute', ns)

            for attr in all_attrs:
                # Check the 'name' of this attribute
                name_node = attr.find('.//eos:name/gco:CharacterString', ns)
                if name_node is not None and name_node.text == target_name:
                    # If name matches, extract the 'value'
                    val_node = attr.find('.//eos:value/gco:CharacterString', ns)
                    if val_node is not None:
                        return val_node.text
            return None

        # 5. Extract Data
        # Pre-calculate EPSG
        epsg_val = find_value('.//gmd:referenceSystemInfo//gmd:referenceSystemIdentifier//gmd:code', use_gco=True)

        metadata = {
            # --- Basical Attributes ---
            'file_identifier': find_value('.//gmd:fileIdentifier', use_gco=True),
            'abstract':        find_value('.//gmd:abstract', use_gco=True),
            'begin_time':      find_value('.//gml:beginPosition', use_gco=False),
            'end_time':        find_value('.//gml:endPosition', use_gco=False),
            'bounding_box':    get_eos_attribute('BoundingBox'),        # e.g. [332370.0, ...]
            'epsg_code':       epsg_val,
            'utm_zone':        get_utm_zone(epsg_val),

            # --- EOS Additional Attributes (Robust Extraction) ---
            'platform':             get_eos_attribute('Platform'),           # e.g. Sentinel-1A
            'burst_id':             get_eos_attribute('BurstID'),            # e.g. t142_303069_iw1
            'track_number':         get_eos_attribute('TrackNumber'),        # e.g. 142
            'sub_swath_id':         get_eos_attribute('SubSwathID'),         # e.g. IW1
            'orbit_pass':           get_eos_attribute('OrbitPassDirection'), # e.g. ascending
            'snap_x (meter)':               get_eos_attribute('BurstGeogridSnapX'),
            'snap_y (meter)':               get_eos_attribute('BurstGeogridSnapY'),

            # --- pipleline
            'input_norm':    get_eos_attribute('InputBackscatterNormalizationConvention'),
            'output_norm':   get_eos_attribute('OutputBackscatterNormalizationConvention'),
            'output_unit':   get_eos_attribute('OutputBackscatterExpressionConvention'),
            'output_db':     get_eos_attribute('OutputBackscatterDecibelConversionEquation'),
            'noise_correction':                   get_eos_attribute('NoiseCorrectionApplied'),
            'radiometric_terrain_correction':     get_eos_attribute('RadiometricTerrainCorrectionApplied'),
            'static_tropospheric_correction':     get_eos_attribute('StaticTroposphericGeolocationCorrectedApplied'),
            'wet_tropospheric_correction':        get_eos_attribute('WetTroposphericGeolocationCorrectionApplied'),
            'bi_static_delay_correction':         get_eos_attribute('BiStaticDelayCorrectionApplied'),
            'filtering_applied':                  get_eos_attribute('FilteringApplied'),
            'preprocessing_multilooking':         get_eos_attribute('PreprocessingMultilookingApplied'),
            'dem_egm_model':                      get_eos_attribute('DemEgmModel'),
            'dem_interpolation':                  get_eos_attribute('DemInterpolation'),
            'geocoding_algorithm':                get_eos_attribute('Geocoding'),
            'geocoding_reference':                get_eos_attribute('GeocodingAlgorithmReference'),
        }

        if verbose:
            print("--- XML Parsing Result ---")
            for k, v in metadata.items():
                print(f"{k:<15}: {v}")

        return pd.DataFrame([metadata])

    except Exception as e:
        print(f"[ERROR] Failed to parse XML: {e}")
        return pd.DataFrame()
