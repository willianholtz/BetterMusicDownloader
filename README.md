# YouTube / Spotify → MP3 (extensão para Vivaldi)

Clique no ícone da extensão em qualquer vídeo do YouTube (ou YouTube Music) — ou
numa faixa, álbum ou playlist do Spotify — e o áudio é baixado e convertido para
**MP3 (VBR ~245–285 kbps, lame -V0)** na sua pasta de música, com os metadados
e a capa embutidos.

```
Vivaldi (extensão)  ──HTTP──>  serviço local 127.0.0.1:8765  ──>  yt-dlp  ──>  ffmpeg  ──>  .mp3
```

## Por que precisa de um serviço local

Um navegador não consegue baixar e converter áudio do YouTube sozinho: o stream
exige decifrar a assinatura do player, e não existe acesso a ffmpeg dentro da
extensão. Então a extensão é só a interface; quem faz o trabalho é um serviço
local (`server/ytflac_server.py`) que roda yt-dlp + ffmpeg.

## Spotify

Link de faixa, álbum ou playlist do Spotify funciona no mesmo botão. O áudio
**não** vem do Spotify (isso exigiria quebrar DRM): o servidor lê os metadados
públicos da página (título, artistas, data, capa), procura o vídeo correspondente
no YouTube casando a **duração** e grava o MP3 com:

- título, artistas, álbum, álbum-artista, data, número da faixa/disco;
- ISRC (Deezer) e capa 640×640 do Spotify;
- link do Spotify em `comment`/`description`.

Álbum e playlist são baixados faixa por faixa, numa subpasta com o nome da
coleção. Álbum/faixa vêm do iTunes (que acerta o lançamento original) e o ISRC
do Deezer — o embed público do Spotify não expõe o álbum de uma faixa isolada.

## Qualidade

O YouTube entrega no máximo **Opus ~160 kbps** (grátis) ou **~256 kbps** (Premium,
com `cookies.txt`). O serviço baixa a melhor faixa disponível e converte para
**MP3 VBR lame -V0** (~245–285 kbps), que é transparente para essa fonte.

- Converter para MP3 **não cria qualidade** que não existia — a fonte já é
  comprimida. O ganho é compatibilidade universal (todo player toca MP3) e
  tamanho razoável (~7 MB por faixa em vez de ~45 MB do FLAC).
- Para qualidade real sem perdas use a fonte original (CD, Bandcamp, Qobuz…)
  ou exporte cookies de uma conta **YouTube Premium** no popup → Configurações.
- O seletor usa `bestaudio/best`: pega o Opus 251 (160 kbps) por padrão.

## Instalação

```bash
cd ~/Documentos/GitHub/youtube-flac
bash install.sh
```

O script:
1. confere `python3` e `ffmpeg`;
2. baixa o `yt-dlp` para `~/.local/bin` se não existir;
3. instala e ativa o serviço `ytflac.service` (systemd `--user`);
4. cria `~/Música/YouTube FLAC`;
5. mostra como carregar a extensão no Vivaldi.

### Carregar no Vivaldi

1. `vivaldi://extensions`
2. ative **Modo do desenvolvedor**
3. **Carregar sem compactação** → escolha `~/Documentos/GitHub/youtube-flac/extension`

## Uso

- Abra o vídeo (ou a faixa/álbum/playlist) → clique no ícone → **Baixar em MP3**.
- Ou clique com o botão direito na página/link → **Baixar áudio em MP3**.
- O popup mostra o progresso; ao terminar, **salvar .mp3** grava o arquivo onde
  você quiser (o arquivo também já está na pasta de destino).
- **abrir pasta** abre a pasta de destino.
- **Nome do arquivo:** `Artista - Música.mp3`, sem ID nenhum no nome. O
  controle de duplicados fica no índice `~/.config/ytflac/downloaded.json`
  (ID da faixa do Spotify / do vídeo do YouTube → caminho do arquivo).
- Se a faixa/vídeo já existe em qualquer subpasta da pasta de destino, o job
  termina na hora (não baixa duas vezes) — dá para recolocar uma playlist
  inteira e ele só completa o que falta. Para forçar de novo, envie
  `{"url": "...", "force": true}` na API.

## Configurações (popup → Configurações)

| Campo | Para quê |
|---|---|
| Pasta de destino | onde os arquivos são gravados |
| cookies.txt | opcional, para vídeos que exigem login/idade (e Premium p/ 256 kbps) |
| Formato de saída | **MP3** (padrão, VBR ~245–285 kbps), **Opus original** (sem conversão, .opus) ou **FLAC** |
| Normalizar volume | aplica loudnorm (EBU R128) — evita músicas "baixas" |
| Qualidade mínima (kbps) | 0 = aceitar tudo. YouTube grátis entrega no máx ~160 kbps. |

O `cookies.txt` pode ser exportado com a extensão "Get cookies.txt LOCALLY".

## API (para depurar)

```bash
curl 127.0.0.1:8765/api/health
curl '127.0.0.1:8765/api/resolve?url=https://open.spotify.com/track/...'
curl -X POST 127.0.0.1:8765/api/jobs -H 'Content-Type: application/json' \
     -d '{"url":"https://www.youtube.com/watch?v=..."}'
curl -X POST 127.0.0.1:8765/api/jobs -H 'Content-Type: application/json' \
     -d '{"url":"https://open.spotify.com/album/..."}'
curl '127.0.0.1:8765/api/jobs?id=<id>'
```

## Manutenção

```bash
# atualizar o yt-dlp (o YouTube muda com frequência)
~/.local/share/ytflac/venv/bin/pip install -U "yt-dlp[default]"

# logs do serviço
journalctl --user -u ytflac -n 50 -f

# reiniciar / parar
systemctl --user restart ytflac
systemctl --user stop ytflac
```

O `yt-dlp` roda numa venv própria (`~/.local/share/ytflac/venv`) porque ela
traz o `mutagen` e os extras usados para embutir título/artista/capa no MP3.

**Runtime JS:** o yt-dlp usa `deno` ou `node` para resolver os desafios do
YouTube. O serviço detecta sozinho (`deno` → `node` → `bun`) e passa
`--js-runtimes`. Sem nenhum deles a extração ainda funciona, mas com menos
formatos disponíveis e um aviso no log.

## Segurança

- O serviço escuta **somente** em `127.0.0.1` (não fica exposto na rede).
- Só aceita URLs de domínios do YouTube.
- Só serve arquivos `.mp3` (e `.flac` antigos) dentro da pasta de destino configurada.

## Estrutura

```
youtube-flac/
├── install.sh
├── README.md
├── server/
│   └── ytflac_server.py
└── extension/
    ├── manifest.json
    ├── background.js
    ├── popup.html
    ├── popup.js
    ├── popup.css
    └── icons/
```
