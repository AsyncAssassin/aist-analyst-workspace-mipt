"""Детерминированные синтетические PDF, без клиентских документов."""
import io
from pypdf import PdfWriter
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject, NumberObject


def pdf_bytes(variant='alpha', pages=2, encrypted=False, scanned=False):
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'):NameObject('/Font'),
        NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    for number in range(1,pages+1):
        page = writer.add_blank_page(width=595,height=842)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream = DecodedStreamObject()
        if scanned:
            # Изображение без текста, аналог сканированной страницы.
            image = DecodedStreamObject(); image.set_data(b'\xff\x00\x00')
            image.update({NameObject('/Type'):NameObject('/XObject'),NameObject('/Subtype'):NameObject('/Image'),
                NameObject('/Width'):NumberObject(1),
                NameObject('/Height'):NumberObject(1),
                NameObject('/ColorSpace'):NameObject('/DeviceRGB'),NameObject('/BitsPerComponent'):NumberObject(8)})
            page['/Resources'][NameObject('/XObject')]=DictionaryObject({NameObject('/Im0'):writer._add_object(image)})
            stream.set_data(b'q 100 0 0 100 72 650 cm /Im0 Do Q')
        else:
            text = f'Synthetic AIST {variant}. Page {number}. Value {100+number}.'
            stream.set_data(f'BT /F1 14 Tf 72 760 Td ({text}) Tj ET'.encode('ascii'))
        page[NameObject('/Contents')]=writer._add_object(stream)
    if encrypted:
        writer.encrypt('synthetic-password')
    output=io.BytesIO(); writer.write(output); return output.getvalue()
