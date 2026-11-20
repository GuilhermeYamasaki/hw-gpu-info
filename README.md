# HW GPU Monitor

Monitor de GPU estilo **HWMonitor** feito em **Python + PySide6 (Qt6)** para Linux / Nobara OS, usando leitura nativa dos sensores `amdgpu` (`sysfs/hwmon`) sem necessidade de `sudo`.

## O que monitora
- **Temperaturas (Atual / Mín / Máx):** GPU (Edge), Hotspot (Junction) e Memória (VRAM)
- **Gráfico em tempo real:** histórico das últimas 120 leituras de GPU (Edge) e Hotspot
- **Clocks (Atual / Mín / Máx):** GPU Core (`sclk`) e Memória (`mclk`)
- **Ventoinha (Fan):** RPM, PWM (%) e indicador visual de status:
  - **FAN OK · GIRANDO:** ventoinha ativa (`RPM > 0`)
  - **ZERO RPM · MODO SILENCIOSO:** `0 RPM` com GPU fria (comportamento normal em idle)
  - **ALERTA · FAN PARADA!:** `0 RPM` com GPU acima de 60 °C (ou Hotspot >= 65 °C)
- **Carga & Energia:** Uso da GPU (%), VRAM usada (MB) e Consumo (W)
- **Bandeja do KDE (System Tray):** ícone dinâmico com a temperatura atual, clique para mostrar/ocultar e opção de minimizar para a bandeja ao fechar
- **Alertas sonoros e notificações:** avisa se a fan parar com a GPU quente ou se a temperatura passar de 85 °C (Edge) / 95 °C (Hotspot), com intervalo mínimo de 30 s entre avisos
- **Histórico automático de jogos da Steam:** detecta quando um jogo da Steam abre e fecha, calcula a média/máxima separada daquela sessão e salva automaticamente em `historico_jogos.json` (acessível pelo botão **Histórico**)
- **Exportação JSON:** botão **JSON** copia todas as métricas atuais, mínimas, médias e máximas para a área de transferência

## Como executar

```bash
./run.sh
```
ou:
```bash
python3 main.py
```

## Criar atalho no menu do Nobara / KDE

```bash
cp hw-gpu-monitor.desktop ~/.local/share/applications/
```
