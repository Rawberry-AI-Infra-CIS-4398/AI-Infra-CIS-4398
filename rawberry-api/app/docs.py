"""Swagger UI with a local, size-aware multipart upload transport."""
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.responses import FileResponse, HTMLResponse


def register_docs(app: FastAPI) -> None:
    @app.get('/static/upload-client.js', include_in_schema=False)
    def upload_client():
        return FileResponse(Path(__file__).parent / 'static' / 'upload-client.js', media_type='text/javascript')

    @app.get('/docs', include_in_schema=False)
    def docs(request: Request):
        prefix = request.scope.get('root_path', '').rstrip('/')
        response = get_swagger_ui_html(
            openapi_url=prefix + app.openapi_url,
            title=app.title + ' - Swagger UI',
            oauth2_redirect_url=prefix + '/docs/oauth2-redirect',
        )
        html = response.body.decode()
        script_url = json.dumps(prefix + '/static/upload-client.js')
        html = html.replace('<script src=', f'<script src={script_url}></script>\n    <script src=', 1)
        html = html.replace('const ui = SwaggerUIBundle({',
                            'const ui = SwaggerUIBundle({\nrequestInterceptor: RAWBerryUploads.requestInterceptor,', 1)
        html = html.replace('<div id="swagger-ui">',
                            '<p style="padding: 0 20px">Bulk uploads are sent in size-limited batches. '
                            'Each file must be at most 25 MiB. If a batch fails, earlier batches remain '
                            'uploaded; the response lists completed and remaining files.</p><div id="swagger-ui">', 1)
        return HTMLResponse(html)

    @app.get('/docs/oauth2-redirect', include_in_schema=False)
    def oauth_redirect():
        return get_swagger_ui_oauth2_redirect_html()
