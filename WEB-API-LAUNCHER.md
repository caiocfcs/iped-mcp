# IPED Web API Multi-Source Launcher

Este pacote sobe uma instancia separada da IPED Web API para cada `source` de um arquivo `multicases.json`. Cada instancia recebe uma porta livre, comecando em `1234`.

## Requisitos

A pasta root do IPED deve conter:

```text
./
|-- jre/
|   `-- bin/
|       `-- java.exe
|-- lib/
|   `-- iped-webapi.jar
|-- python/
|   `-- python.exe
|-- multicases.json
`-- web-api-launcher.py
```

No Linux, o script procura `jre/bin/java` em vez de `java.exe`.

Python 3.9 ou mais recente e recomendado.

## Uso mais simples

Coloque `web-api-launcher.py` e `multicases.json` na pasta root do IPED e execute:

```powershell
python/python.exe web-api-launcher.py
```

Sem argumentos, o script usa:

- root: diretorio atual (`./`)
- sources: `./multicases.json`
- host: `0.0.0.0`
- primeira porta: `1234`

## Informando a pasta root e o JSON

```powershell
python web-api-launcher.py --root "C:\caminho\para\iped" --sources "C:\caminho\multicases.json"
```

Tambem e possivel alterar host e porta inicial:

```powershell
python web-api-launcher.py --root "C:\caminho\para\iped" --sources ".\multicases.json" --host 0.0.0.0 --start-port 1234
```

Para ver todas as opcoes:

```powershell
python web-api-launcher.py --help
```

## Formato do multicases.json

```json
[
  {"id": "src1", "path": "path\\to\\case1"},
  {"id": "src2", "path": "path\\to\\case2"}
]
```

O arquivo precisa conter uma lista nao vazia. Cada item deve ter `id` e `path`. Outros campos eventualmente aceitos pelo IPED sao preservados no JSON individual.

## Funcionamento

1. O script valida o Java, o JAR e o `multicases.json`.
2. Para cada source, cria temporariamente um JSON contendo somente aquela source.
3. Procura uma porta livre a partir de `1234`. Portas ocupadas sao puladas.
4. Sobe um subprocesso Java independente para cada source.
5. Mostra no terminal a source, a URL e o PID de cada servidor.
6. Mantem o processo Python ativo e monitora os subprocessos.
7. Ao receber Ctrl+C, fechamento normal ou sinal de termino, encerra todos os servidores e remove os JSONs temporarios.

No Windows, os processos Java sao associados a um Job Object configurado com `KILL_ON_JOB_CLOSE`. Isso faz o sistema encerrar a arvore de processos quando o processo Python termina ou quando a janela do terminal e fechada. Em Linux/macOS, cada Java usa um grupo de processos separado, encerrado pelo script com sinais de termino.

## Exemplo de saida

```text
[INICIANDO] source=src1 porta=1234
[PORTA] 1235 ocupada ou indisponivel; tentando 1236.
[INICIANDO] source=src2 porta=1236

=== SERVIDORES IPED INICIADOS ===
- src1: http://0.0.0.0:1234/  (PID 14220)
- src2: http://0.0.0.0:1236/  (PID 15304)

Pressione Ctrl+C para encerrar todos os servidores.
```

A saida nativa de cada processo Java permanece no mesmo terminal.

## Encerramento

Use:

```text
Ctrl+C
```

O script encerra todas as instancias. Fechar a janela do terminal tambem encerra os processos no Windows por meio do Job Object.

> Observacao: nenhum programa consegue executar limpeza graciosa depois de uma queda total do sistema, desligamento forcado ou `kill -9`. Nesses casos, o proprio sistema operacional encerra os processos e libera as portas. O Job Object cobre o caso importante no Windows em que o processo Python ou o terminal e encerrado abruptamente.

## Observacoes

- A verificacao inicial de porta reduz conflitos, mas outro processo ainda poderia ocupar a porta no intervalo minimo entre o teste e a abertura pelo Java. Se a API Java falhar imediatamente, o launcher informa o erro e encerra todas as instancias ja iniciadas.
- O script espera 350 ms para detectar falhas imediatas. A listagem indica que o processo continuou ativo, nao que um endpoint HTTP especifico respondeu.
- Por padrao, a API escuta em todas as interfaces usando `0.0.0.0`. Para limitar o acesso ao proprio computador, use `--host 127.0.0.1`. Observe as regras de firewall e seguranca do ambiente.
