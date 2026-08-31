#!/bin/sh
# ImageMagick по умолчанию запрещает читать PDF: liteparse рендерит их через magick
set -eu
sed -i 's#</policymap>#  <policy domain="coder" rights="read|write" pattern="PDF"/>\n</policymap>#' \
    /etc/ImageMagick-6/policy.xml

# tesseract ищет пары языков файлами; данные приезжают в образ позже симлинков
mkdir -p /usr/share/tessdata
ln -sf eng.traineddata /usr/share/tessdata/rus+eng.traineddata
ln -sf rus.traineddata /usr/share/tessdata/eng+rus.traineddata
