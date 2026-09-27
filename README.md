# BotMessari

Agente local de curadoria cripto: briefing diario por Telegram, relatorio semanal e rascunhos de posts para o X (todos revisados por voce antes de publicar).

## Componentes

| Script | O que faz | Como roda |
|---|---|---|
| `crypto_daily_agent.py` | **Agente principal (diario/semanal).** Mercado (Messari, CoinGecko, CMC), Fear & Greed, trending, gainers/losers, funding/OI da Binance, narrativa via Messari AI. Semanal: funding rounds e airdrops via CryptoRank. Envia por Telegram. | Tarefa do Windows as 07:10 (`run_daily.ps1`), GitHub Actions, `npm run once` |
| `messari_daily_agent.py` | Variante focada so na Messari: research reports (API ou indice publico), news, newsletter/podcast. Dedup de research ja enviados. | `python .\messari_daily_agent.py --send-telegram` |
| `x_generator.py` | Gera 1-2 rascunhos de post para o X (ingles) cruzando RSSs, YouTube, Lookonchain e influencers. Nao posta automaticamente: grava em `x_posts/` e envia no Telegram para voce revisar. | `run_x.ps1` ou `python .\x_generator.py --send-telegram` |

## Configuracao

1. Copie `.env.example` para `.env` e preencha:

```env
MESSARI_API_KEY=sua-chave-da-messari-aqui
TELEGRAM_BOT_TOKEN=token-do-seu-bot-telegram
TELEGRAM_CHAT_ID=seu-chat-id
COINGECKO_API_KEY=opcional (melhora limites da CoinGecko)
COINMARKETCAP_API_KEY=opcional (Fear & Greed da CMC)
CRYPTORANK_API_KEY=opcional (funding/airdrops no modo semanal)
OPENROUTER_API_KEY=opcional (usado pelo x_generator; cai para Messari AI sem ele)
```

2. Instale a dependencia opcional (transcricao de YouTube para os posts do X):

```powershell
pip install -r requirements.txt
```

## Uso

```powershell
npm run once      # roda o agente diario uma vez
npm run dev       # roda agora e depois diariamente as 07:10 (deixe o terminal aberto)
npm run schedule  # instala a tarefa do Windows "BotMessariDailyTelegram" as 07:10
npm run test      # roda os testes (pytest)

python .\x_generator.py --send-telegram   # gera rascunhos para o X e envia no Telegram
python .\x_generator.py --dry-run         # so gera, sem enviar e sem gravar state
```

### Opcoes uteis do agente diario

```powershell
python .\crypto_daily_agent.py --send-telegram --daily
python .\crypto_daily_agent.py --send-telegram --weekly
python .\crypto_daily_agent.py --assets bitcoin,ethereum,solana
python .\crypto_daily_agent.py --no-ai
python .\crypto_daily_agent.py --dry-run --send-telegram
```

## GitHub Actions (backup na nuvem)

`.github/workflows/run-bot.yml` roda o agente diario as 10:10 UTC (07:10 BRT) e o semanal aos domingos as 11:00 UTC, usando os secrets do repositorio. Voce tambem pode disparar manualmente (Run workflow) escolhendo o modo `daily` ou `weekly`.

## Como evita repetir conteudo

- `state/crypto_agent_state.json`: trending (6h), funding/airdrops (7d), narrativa (30d)
- `state/messari_agent_state.json`: research reports e itens publicos ja enviados
- `state/x_generator_state.json`: historias postadas nas ultimas ~36h, com dedup por similaridade de tokens (detecta a mesma noticia em fontes diferentes)
- Preco, volume e Fear & Greed mudam todo dia, entao continuam aparecendo

## Robustez

- HTTP com retry automatico (GET com backoff exponencial em 429/5xx/erro de rede; POST nunca e reenviado para nao duplicar mensagem)
- Lockfile: se duas execucoes rodarem em paralelo (tarefa do Windows + CI + manual), a segunda sai sem enviar duplicado (arquivos `state/.lock_*`)
- O registro de conteudo enviado acontece ANTES do envio no Telegram; se o envio falhar, o state e revertido
- `run_daily.ps1` grava logs em `logs/` (mantem os 14 mais recentes) e envia um alerta no Telegram se o agente falhar
- Se um endpoint nao estiver liberado no seu plano, o relatorio informa o erro e continua com os dados restantes

## Estrutura

```
crypto_daily_agent.py    agente principal (diario/semanal)
messari_daily_agent.py   variante Messari-only
x_generator.py           rascunhos para o X
lib/common.py            HTTP+retry, Telegram, state, lock, logging, helpers
run_daily.ps1            wrapper da tarefa do Windows (com logs e alerta)
run_x.ps1                wrapper do gerador de posts
reports/                 relatorios Markdown gerados
x_posts/                 rascunhos de posts gerados
state/                   deduplicacao e lock
logs/                    logs rotativos
```

Aviso: informativo, nao e recomendacao financeira.
