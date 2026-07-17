# Tradam Scalping Bot

Bot de trading algorithmique educatif pour MetaTrader 5, concu pour apprentissage, backtest, paper trading et compte demo. Il ne promet aucune rentabilite et ne doit pas etre utilise comme conseil financier.

Le mode par defaut est `paper`. Le trading reel est bloque par defaut et necessite explicitement `ENABLE_LIVE_TRADING=true` et `LIVE_TRADING_CONFIRMATION=true`, plus une configuration `mode: live`.

## Ce que contient cette version

- Connexion MetaTrader 5 via le package officiel `MetaTrader5` quand il est disponible.
- Verification de compte demo avant tout mode `demo_live`.
- Mapping automatique des symboles broker pour `XAUUSD`, `BTC` et `DJ30`.
- Donnees multi-timeframe `M5`, `M15`, `H1`, `H4`.
- Indicateurs: EMA20, EMA50, EMA200, RSI14, ATR14, MACD, ADX, Bollinger Bands, VWAP, supports/resistances, pivots journaliers.
- Fibonacci: detection de swing, retracements 23.6/38.2/50/61.8/78.6 et extensions 127.2/161.8.
- Moteur de scoring explicable avec confluence technique, news, spread, volatilite et regime de marche.
- Risk manager strict: SL/TP obligatoires, sizing automatique, pertes max, drawdown, pertes consecutives, pas de martingale, pas de grid agressif.
- News optionnelles: Alpha Vantage, Marketaux, Finnhub, Financial Modeling Prep, NewsAPI, Binance public API pour contexte BTC.
- SQLite pour trades, decisions, news et sessions.
- Rapports HTML, CSV et JSON.
- Backtester CSV simple.
- Tests unitaires de base.

## Installation

```bash
cd trading-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Sur Windows avec MetaTrader 5 installe, `pip install -r requirements.txt` installe aussi `MetaTrader5`. Sur macOS/Linux, cette dependance est ignoree automatiquement car le terminal MT5 officiel n'est pas disponible de la meme facon.

## Configuration

Fichiers principaux:

- `config/settings.yaml`: mode, chemins, timeframes, seuils de strategie, garde-fous live.
- `config/symbols.yaml`: alias broker, spreads max, requetes news par actif.
- `config/sessions.yaml`: sessions Asian/US configurables et blocs de faible liquidite.
- `config/risk.yaml`: risque, sizing, limites de pertes, validation execution.
- `.env`: identifiants MT5 et cles API optionnelles.

Variables sensibles a mettre dans `.env`, jamais dans le code:

```bash
TRADING_MODE=paper
ENABLE_LIVE_TRADING=false
LIVE_TRADING_CONFIRMATION=false
MT5_LOGIN=
MT5_PASSWORD=
MT5_SERVER=
ALPHA_VANTAGE_API_KEY=
MARKETAUX_API_KEY=
FINNHUB_API_KEY=
FMP_API_KEY=
NEWSAPI_API_KEY=
```

## Lancer en paper mode

```bash
source .venv/bin/activate
python -m src.main --mode paper --once
```

En `paper`, aucun ordre reel n'est envoye. Si MT5 est disponible, le bot peut scanner les donnees du terminal et simuler les ordres. Si MT5 n'est pas disponible, il sort proprement sans trader.

## Lancer en demo MT5

1. Ouvrir MetaTrader 5.
2. Se connecter a un compte demo.
3. Verifier que les symboles broker sont visibles dans Market Watch.
4. Renseigner `.env` si necessaire.
5. Lancer:

```bash
python -m src.main --mode demo_live --once
```

Le bot bloque l'execution si le compte detecte n'est pas demo.

## Trading reel

Le trading reel est volontairement desactive. Meme si vous passez `--mode live`, l'execution est bloquee tant que ces deux variables ne sont pas actives:

```bash
ENABLE_LIVE_TRADING=true
LIVE_TRADING_CONFIRMATION=true
```

La configuration par defaut contient aussi `require_demo_account: true`, ce qui bloque les comptes reels. Ne modifiez cela qu'apres une longue phase de backtest, paper trading et forward test demo.

## Backtest CSV

Placez les historiques dans `data/backtest/` avec ce format de nom:

```text
XAUUSD_M5.csv
XAUUSD_M15.csv
XAUUSD_H1.csv
BTC_M5.csv
DJ30_M5.csv
```

Colonnes attendues:

```text
time,open,high,low,close,volume
```

Puis lancez:

```bash
python -m src.backtest.run_backtest --data-dir data/backtest --output reports/backtest_summary.json
```

Le backtest produit le nombre de trades, winrate, profit factor, drawdown max, expectancy, performance par symbole, session et type de signal.

## Rapports

Generer un rapport exemple fictif:

```bash
python -m src.main --report-example
```

Generer un rapport reel depuis les trades, decisions et news deja stockes en SQLite:

```bash
python -m src.main --mode demo_live --session-report --report-session US
```

Generer un rapport reel apres reconciliation MT5:

```bash
python -m src.main --mode demo_live --session-report --reconcile-mt5 --report-session US
```

Par defaut, le rapport reel couvre la journee UTC en cours. Pour choisir une fenetre precise:

```bash
python -m src.main --mode demo_live --session-report --report-session US --report-start 2026-07-17T00:00:00+00:00 --report-end 2026-07-17T23:59:59+00:00
```

Migrer la base SQLite sans lancer le bot:

```bash
python -m src.main --migrate-only
```

Supprimer les anciens doublons `example-001` apres sauvegarde automatique:

```bash
python -m src.main --cleanup-example-fixtures
```

Les rapports sont crees dans `reports/`:

- HTML lisible.
- CSV des trades.
- JSON resume, utile pour une analyse externe ou ChatGPT.

Chaque decision contient les raisons d'acceptation ou de refus, le score, les indicateurs, les news actives et le contexte de risque.

Les rapports normaux excluent les fixtures (`is_fixture=false`) et filtrent par `mode`, `run_id`, `session_id` et plage temporelle quand ces valeurs sont fournies.

## Systeme de scoring

Exemples de points:

- Tendance H1 alignee: `+2`.
- Confirmation M15 autour EMA20: `+1`.
- Pullback/reclaim EMA20: `+1`.
- RSI autour de 50 dans le bon sens: `+1`.
- MACD histogram confirme: `+1`.
- Fibonacci 38.2/50/61.8 en confluence: `+2`.
- Support/resistance ou breakout confirme: `+1`.
- ADX suffisant: `+1`.
- News favorable: `+1`.
- News conflictuelle: penalite.

Rejets automatiques:

- Spread trop eleve.
- News economique majeure dans la fenetre de blocage.
- ADX trop faible.
- Range trop serre.
- Volatilite insuffisante ou anormale.
- Ratio risk/reward trop faible.
- SL/TP manquant.

## Tests

```bash
source .venv/bin/activate
pytest -q
```

Couverture de base:

- Indicateurs.
- Fibonacci.
- Calcul de lot.
- Risk manager.
- Filtre de session.
- Filtre news.
- Signal scoring.
- Validation d'ordre.
- Generation de rapport.

## Architecture

```text
src/
  mt5/         connexion, donnees, ordre, mapping symboles
  strategy/   indicateurs, Fibonacci, scoring, risque, sessions, trade management
  news/       APIs optionnelles, calendrier economique, sentiment, filtres
  analytics/  logs de trades, metriques, rapports, graphiques HTML
  storage/    SQLite et modeles
  backtest/   backtester CSV simple
  utils/      configuration, logs, temps, exceptions
```

## Limites importantes

- Cette version est une base robuste de recherche et de forward test, pas une strategie garantie.
- Les APIs gratuites peuvent etre limitees, retardees ou indisponibles.
- Le backtester est volontairement simple et ne remplace pas une simulation tick-by-tick avec slippage, commissions et conditions broker.
- Les seuils par defaut doivent etre calibres par symbole, broker, spread, session et volatilite.
- Les news et le sentiment sont des filtres d'aide a la decision, pas une source de verite.

Risque: le trading comporte un risque eleve de perte. Utilisez d'abord le mode `paper`, puis un compte demo. Ne tradez jamais en reel avec un bot non audite et non teste sur une longue periode.
