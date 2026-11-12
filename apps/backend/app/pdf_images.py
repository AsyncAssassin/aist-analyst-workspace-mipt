"""Full-page images from authenticated PDF bytes, only for the versioned Fable extractor."""
import base64
import hashlib
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time

from .extraction_requests import failure
from .runs import json_value

QUALIFICATION = ('Изображения выбранных страниц переданы извлекателю. Сведения, видимые только на изображении, '
                 'не являются независимо проверенными числовыми основаниями: число, субъект, единица, период '
                 'и исторический статус требуют подтверждения сохраненным текстом или поддержанной структурой. '
                 'Отсутствие такого основания не означает отсутствия сведений в PDF.')


def render(data, pages, parameters, seconds):
    """No database connection or Run lock is held here. One deadline bounds all selected pages."""
    deadline = time.monotonic() + min(60, max(0, seconds))
    images = []
    try:
        with TemporaryDirectory(prefix='aist-pdf-pages-') as directory:
            source = Path(directory) / 'source.pdf'
            source.write_bytes(data)
            for page in pages:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired('pdftoppm', seconds)
                target = Path(directory) / str(page)
                process = subprocess.run(['pdftoppm', '-f', str(page), '-l', str(page), '-singlefile',
                    '-scale-to', str(parameters['long_side']), '-jpeg', '-jpegopt', 'quality='+str(parameters['quality']),
                    str(source), str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=remaining, check=False)
                if process.returncode:
                    raise OSError('Page renderer failed')
                content = target.with_suffix('.jpg').read_bytes()
                if not content.startswith(b'\xff\xd8') or not content.endswith(b'\xff\xd9'):
                    raise OSError('Invalid JPEG')
                images.append(dict(page=page, sha256=hashlib.sha256(content).hexdigest(), byte_size=len(content),
                                   data=base64.b64encode(content).decode('ascii')))
    except (OSError, subprocess.TimeoutExpired):
        raise failure('Полные изображения выбранных страниц не подготовлены в отведенный срок. '
                      'POST не отправлен; текстовый обход не выполняется. Используй сохраненные основания '
                      'и укажи ограничение визуального извлечения.') from None
    return images


def attach(request, pin, images, parameters):
    blocks = [dict(type='text', text=request['messages'][-1]['content'])]
    for item in images:
        mapping = dict(source=pin, page=item['page'], image_sha256=item['sha256'], render=parameters)
        blocks.extend([dict(type='text', text=json_value(mapping)),
                       dict(type='image', source=dict(type='base64', media_type='image/jpeg', data=item['data']))])
    return dict(request, messages=[*request['messages'][:-1], dict(role='user', content=blocks)])
