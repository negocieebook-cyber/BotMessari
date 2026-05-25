# BotMessari

Agente local para gerar um resumo cripto diario usando sua chave da Messari.

## Como usar

1. Crie um arquivo `.env` na raiz:

```env
MESSARI_API_KEY=sua-chave-da-messari-aqui
TELEGRAM_BOT_TOKEN=token-do-seu-bot-telegram
TELEGRAM_CHAT_ID=seu-chat-id
```

2. Rode o agente localmente em modo continuo:

```powershell
npm run dev
```

Esse comando executa uma vez ao iniciar e depois fica aguardando para rodar de novo todos os dias as 07:10. Deixe o terminal aberto. Para parar, use `Ctrl+C`.

Se o PowerShell bloquear `npm.ps1`, rode:

```powershell
npm.cmd run dev
```

Para rodar apenas uma vez, use:

```powershell
npm run once
```

Ou diretamente pelo Python, sem envio para o Telegram:

```powershell
python .\messari_daily_agent.py
```

3. O Markdown sera salvo em `reports/`.

4. Para mandar tambem no Telegram:

```powershell
python .\messari_daily_agent.py --send-telegram
```

## Opcoes uteis

```powershell
python .\messari_daily_agent.py --assets bitcoin,ethereum,solana --research-limit 8
python .\messari_daily_agent.py --tags defi,stablecoins
python .\messari_daily_agent.py --no-ai
python .\messari_daily_agent.py --no-ai-research-fallback
python .\messari_daily_agent.py --no-public-research-fallback
python .\messari_daily_agent.py --no-public-news
python .\messari_daily_agent.py --no-public-podcast
python .\messari_daily_agent.py --send-telegram
npm run test
npm run once
npm run schedule
```

## Agendamento diario as 07:10

Depois de configurar o `.env`, instale a tarefa do Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_windows_task.ps1
```

A tarefa criada se chama `BotMessariDailyTelegram` e executa `run_daily.ps1` todos os dias as 07:10 no horario local do Windows.

## Como nao repetir dados

O agente guarda os reports de Research ja enviados em `state/messari_agent_state.json`.
Em execucoes futuras, reports com o mesmo `id` ou mesmo titulo/data sao pulados. Os dados de mercado continuam aparecendo diariamente porque preco, volume e variacoes mudam a cada dia.

O agente tenta usar:

- `GET /research/v1/reports`
- `GET /research/v1/reports/{reportId}`
- `GET /metrics/v2/assets/details`
- `POST /ai/v1/chat/completions`
- `POST https://api.telegram.org/bot.../sendMessage`
- paginas publicas: `https://messari.io/news` e `https://messari.io/research/newsletter-and-podcast`
- podcast RSS publico: `https://anchor.fm/s/fb66e238/podcast/rss`

Se o endpoint direto de Research nao estiver liberado no seu plano da Messari, o agente tenta ler indices publicos da Messari, News, Newsletter/Podcast e, depois, preencher a secao com um fallback via Messari AI. A leitura publica pode receber `429 Too Many Requests`, porque depende das paginas web abertas da Messari e nao de uma API garantida. Se algum endpoint nao estiver liberado, o relatorio informa o erro e continua com os dados restantes.
