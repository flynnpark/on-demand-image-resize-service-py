import base64
import io
import traceback
from typing import Dict, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import boto3
from PIL import Image, ImageOps

S3_STAGING_BUCKET_NAME = 's3_staging_bucket_name'
S3_PRODUCTION_BUCKET_NAME = 's3_production_bucket_name'

s3_client = boto3.client('s3')


def get_s3_object(bucket_name: str, object_key: str) -> dict:
    return s3_client.get_object(Bucket=bucket_name, Key=unquote(object_key))


def put_s3_object(bucket_name: str, object_key: str, content_type: str, body: str) -> dict:
    return s3_client.put_object(
        Bucket=bucket_name,
        Key=unquote(object_key),
        ContentType=content_type,
        Body=body,
    )


def transform_querystring(querystring: str) -> Optional[Dict[str, int]]:
    origin_size_info = dict(parse_qsl(urlsplit(querystring).path))
    if not any(key in origin_size_info for key in ('w', 'h')):
        return None

    for key, value in origin_size_info.items():
        origin_size_info[key] = int(value) if value else None

    return origin_size_info


def resize_image(original_image: Image, size_info: Dict[str, int]) -> Image:
    target_width = size_info.get('w')
    target_height = size_info.get('h')

    fixed_image = ImageOps.exif_transpose(original_image)
    width, height = fixed_image.size
    transform_ratio = 1.0

    if target_width and target_height:
        w_decrease_ratio = target_width / width
        h_decrease_ratio = target_height / height
        transform_ratio = max(w_decrease_ratio, h_decrease_ratio)

    elif target_width:
        transform_ratio = target_width / width

    elif target_height:
        transform_ratio = target_height / height

    resized_image = fixed_image.resize((int(width * transform_ratio), int(height * transform_ratio)), Image.ANTIALIAS)

    resized_width, resized_height = resized_image.size
    if target_width and target_height and (target_width != resized_width or target_height != resized_height):
        start_x = (resized_width - target_width) / 2
        start_y = (resized_height - target_height) / 2
        end_x = start_x + target_width
        end_y = start_y + target_height
        crop_coords = (start_x, start_y, end_x, end_y)
        resized_image = resized_image.crop(crop_coords)

    bytes_io = io.BytesIO()
    resized_image.save(bytes_io, format=original_image.format, optimize=True, quality=95)
    original_image.close()

    image_size = bytes_io.tell()
    image_data = base64.standard_b64encode(bytes_io.getvalue()).decode()
    bytes_io.close()

    return {'size': image_size, 'data': image_data}


def handler(event, context):
    request = event['Records'][0]['cf']['request']
    response = event['Records'][0]['cf']['response']

    s3_bucket_name = S3_STAGING_BUCKET_NAME if 'staging' in context.function_name else S3_PRODUCTION_BUCKET_NAME

    try:
        if int(response['status']) != 200:
            return response

        s3_object_key = request['uri'][1:]
        s3_object = get_s3_object(s3_bucket_name, s3_object_key)
        if not s3_object:
            return response

        s3_object_content_type = s3_object['ContentType']
        if s3_object_content_type not in ['image/jpeg', 'image/jpg', 'image/png']:
            return response

        size_info = transform_querystring(request['querystring'])
        if size_info is None:
            return response

        original_image = Image.open(s3_object['Body'])
        resized_result = resize_image(original_image, size_info)

        if resized_result['size'] > 1024 * 1024:
            s3_object_key_split = s3_object_key.split('/')
            s3_object_key_split[-1] = f'resized_{urlencode(size_info)}_{s3_object_key_split[-1]}'
            converted_object_key = '/'.join(s3_object_key_split)

            put_s3_object(s3_bucket_name, converted_object_key, s3_object_content_type, resized_result['data'])

            response['status'] = 301
            response['statusDescription'] = 'Moved Permantly'
            response['body'] = ''
            response['headers']['location'] = [
                {
                    'key': 'Location',
                    'value': f'/{converted_object_key}',
                }
            ]
            return response

        response['status'] = 200
        response['statusDescription'] = 'OK'
        response['body'] = resized_result['data']
        response['bodyEncoding'] = 'base64'
        response['headers']['content-type'] = [{'key': 'Content-Type', 'value': s3_object_content_type}]
        return response

    except Exception as e:
        print(traceback.format_exc())
        print(e)

    finally:
        return response
