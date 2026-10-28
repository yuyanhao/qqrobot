#!/usr/bin/env bash
set -euo pipefail
APP_DIR=/opt/qq-math-bot
if [[ ${EUID} -ne 0 ]]; then
  echo '请用 sudo bash 运行此命令。'
  exit 1
fi
if [[ ! -x "$APP_DIR/.venv/bin/python" || ! -f "$APP_DIR/.env" || ! -f "$APP_DIR/data/tasks.sqlite3" ]]; then
  echo '未找到 /opt/qq-math-bot 默认安装，尚未修改程序。'
  exit 1
fi
if [[ "$(systemctl show qq-math-bot -p User --value)" != qqbot ]]; then
  echo '服务运行账户不是预期的 qqbot，尚未修改程序。'
  exit 1
fi
update_pdf() {
  "$APP_DIR/.venv/bin/python" - "$APP_DIR" "$1" <<'UPDATE_PDF_PY'
from pathlib import Path
import ast
import base64
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import zlib

target = Path(sys.argv[1]).resolve()
phase = sys.argv[2]
if phase not in ("check", "install"):
    raise SystemExit("更新阶段无效，未修改程序。")
payload = json.loads(zlib.decompress(base64.b64decode("eNrFXHt3E9e1/ypT3T8iEVlIfvBwarIIOCkrhLAI6c2t5aU1lkb2FEmjzIwAx/gupwlgBwi0CQkB0kIvadKUAE3TxAFD1upHST2y/Ve+wv3tfc7MnHlIttP0XhKQNOe1zz77vfeZuUy7Vq+4ltVwCu3ZzKiWKfN/3XfeWX/ynnf2E+3owee1tetvdy+e9+5d37j99+7yl90/vNddvKIdPfLCDysXV5/cWP/03PqnS93Pbnu/v7B+/9zGH6/9c+E3Yp5yq9yq21ZTq1TqHbdjG5WKZjbblu1qeqtlubprWi2HesmnphV8bVjT02ZrOvjd1N2Z4IdtBF/dGdvQa2rPTsusWjWjpru6XL6NsQ1zyl/7KE8V9G/PAgtmpzmo6Y4mvspxRw8d9sccaurTBg0CXNqYD11h2nAP46thZ8uZ118fqBlG2zGMEwNTllvAVOVMjsYAi4defaly+OUDL2JsAHHh2GGreiKLLi/tf61ydP8L45Wjh14bP/wKOu2qFItF+isan9t//MAvwtbSnmjzK4cOKmOHdvnPcUiV5/7r+Dg9Ha6MqEMAU9A0UtR2aKXi4LD8KLcENAf2Hzl46OD+4+OVY+PoZhuFqtVsmw0ja5cz2Wd//rOJcrlWmMxlnx1du3u3XHZ25J6deHpg8lk8piY8KaABdPN0uTzw/cJ73y+8/98/PP79+vm/eYsP/rnwZt773UXv8js7J3egDyNLLHzk1ZeeGz9GS9I6ycmfJtoSXY/tP/ICAwdkz6mj52kIBtPMPBjfJxJATFJb/3G5Z8PVXjk6fiCJjHBphmaeVgp3py4h2nM7lL0eePnVI8fTETzaXXjknf3rmdXlBfrA3+7NpTPelcX1e7fPoI0+qAFfiTevPsCQ1eWHZ7rX7p8ZzT17Br1ztDhjDJ8Bkg/84tCR8VfGJUzJpSXCJzZufP3PhfNYfvXhxdXlJe/GjdWH73tnv1hdfss7+5fVbz/2Lr259tET79Jbq8vodmcyusyx8f0H01FWbmn4IzbpXf58/f6jM6uPztIHfq3dvEC/6ANPvMsfnNm4Rm05cTJHf+60jeq+n4DocpI/y61qQ3ccknbjtm3Z2WOdlms2Df6RGxXACpFGAnF1+cL648felXe7H97q/u1qd+k9b2VhpxCNEdlXbtWMulZxjIZRdSttSBAn6xqn3VHNce285kICNkY1s+XmId5Oiw78G8gq5rSBfZrbaTeMiYbpuBN4PJnXpiCsJyMQvcKza+2ZWces6g0W2TzTM9obhm1pTUNvOZreaEDm1jTjdLthVk3IV7Npuo7WMk4atubanVZVd42CDzufjvF6x3BcowZwDbE+4MJXkljUgbaCJ4q8LbQsu6k3zDdwwpkjz794oJzJi26WzeDKkSawEqPBgmPodnWG8eOjnKHQTccIT6acWb//zdr7n21c+657YWl15T6o3vviw0BBrd16k9XSBe9Pv/l+4SYIYQhPv1/4uLv4gfw9MCKeBND8BzTCtKFVLZy6o9UMp2qbU4amazWr2mkadDw2VAbhaUZvaXqNhLfAMc4QDw1NHDG0WSGCmiiHF5zOFDag+UiRy9eBG2i36gxOXovwTKFutmqmC+0SxwpxgFnHedOaWIjHF6Ztq9PGCtSK3RVAZSb91vIg+2C78gBoygkxzmjVsrnRSfTXbdc5ZbozWYyCJidI8eXDexisLk9/qhZ4pNUxIpNCqUdho7nFjJiH5UJsmsT5ikPs3lrxVi4Tg11dxIHi0L1zH0UONHaawfkOYq8j+LtnoFSMHbQ4JaMGhD2vNxwjDf8JxffjDiGXQEtEgRTqnUaD+2cjs/x/YieGoeO2eraEpLZOJlqL5LgDKeKSpojoOVBL392AFtqWSUzGyiDEQbSbVA0s7QONL4V/LxWudIS+Jp2XySdnpR0UbKPd0KskoUh/MIUTs4SPmU7l41x0EuYQQI9NZIPdyCMvxTujQ4+ugzkSiDxZbIwklmxJ+/mYXA1fWFOw/ObnNLH/NI7jFKqpC7UFS0Gb4zHzGrYIuhHE5H13duP2I+/mZ1ppwG9f/fbCxodf/bByo3vzc6GDN976zFs8F0hYkNzG+UvdDx4IyRshoojyKHTa0AywKvTWtJFtmq0s7ypPm8ix3lMfaE9rARqFgAUWYH8btWwwY46QFBCqAT7WSEHKFUpSr6ozoT+dQqBjc9o+rcjobBitbPAo2qe3DgI+uzfvdu/e3ri5IJCjzYUT+chd//rs+nfnN85eWnt8Dy6UNhdMzT1Wl9/Z+OgKMAxMChyuP3nLe/DbAMPAfHfx8+4HX4glAgzbBjyplsBOPsBDaG60W9OVqVkXpoZJPsuocF0K/K+wM7jVtzMibgLbHKJZNTHGW6TgNatl8LLPAIZap4of5mmgH7/aBo641pjVyG5qsEbUq24Hp0CTaVW9rc1YjZoTsS+qHds2GAYGFLZhezYwLezZ0ajwqbDkEUc8GCf6qU69DvWMmazCcwT+oZezMYKUqxUc/aSRFf3zNDEE0Bj448gLxPJWG2YfrJcxEn6xCci+wQpiKDl+J/VGx8gm+ZdIgTrniEdDfKewqThJ4aiqDafMmjuT12YMc3qG8BPADtASyxFLRQZgXa00uCdlwSk4nidiAg3mWoNR5y9iG7RMljiBmImYgqeHb1jYPZKTPCtbJIiyCW2C2I4Zjt6EioCPe3j/kQO/evmVHqdRbViO0eOoiPUFdH2sQbDLxsd/9G48WVs67717YW3lz7DLVx9dEtznXf7txsKb3p1PvQeXwW3e4tcBG0GrY26VyhIw+Rzl6HWDObfiwMA2mM/zWq1tStud2aAy1amBKPgRs1G9YemunD5+oDQDkVCFUR1KKRL9gCpLwY6C6QBGGB5ZJrQcy6sizpZ/Em2VhoeLRWYO8QgMEqOEvoY06QSJvgcPvfvL3spvvOXl0FiivZJVA/rCXrWd2u7BPIdhCs7rtptVd43GgEj8pfNaPDKx08dDskEOCuzxg88jCqNBV8J61AwdtpljQgJ12s9AsLChzvt2Z3BU8GlcW/eF0ZSBBoOwaJERAgqURzADv1OAXzXMRgAubzOHL2FTQNSybV/0hBVriVG0Y0wr7N27NyKduUVxAUFYNcOWLmDoVrQ6zSnDhixWPDyfrpiI+DnLD9/pi6BdnE4seiTwG4kY7dzpMy31H86zjJKLg219AmxKjTsxGVrGohfRlg9suH/aDrr7+5mQfQe00mTYKSrKWQyZbhMaQbKBQE2WMTbWk9WiXBa3tJJrBPshkc3rFVyrgthDNgdF04LT64IFjr3wXMJySQqGKNzpMqsPBE5Bb7fJxYpp5tyWF5b6Mb6ucbpqtN2ApdNch0Tncf4gfwXBTjzr729IyxF2sjYnDpdtFxHq8O78df2rT4A/jYOlmEzR2Kl7aSe2IdlF4CnkF59danXpGpCOHBWWST70sDmOggMmm0I+TsZRZIPPVhQ93UUP4/EVwWV5IdL7RlyOMXQcUgnMUF6zoB3W7WkSxO0OnBxnxuo0aloHGA03VJmSriZMWb0WMYgEFqT9mo9beHmYQGOBJOElGEGEGhFPUKJIzDNpOO41exruBahZip6PctA8n8R6GsKTuNbS/lj1uiMVphjGct3/Pbyd8+Gn6YeEzIHlkLbo2LHTYoUK21VkEQyN3NEwkCOgiZ6PcCME3GRlFcmLI53NXhm18SA2/Ib7xrA4uyJ9+aVv4UoIXz5u5CdPux0cQ/y08ypweQWaXFIPhfM5wEvVkKylnYkfc/x8FbOnx6n2PdYz2hFyIsb4wzdOyAZiN48goRgUAoz4YNuIce4Qs+gt+OaiT56hzEnXD5pMPI6aUTyanGz63KdFUh6bWUWIoa/9+SFiJsKKpPzIS6GioGiWpqR0RvvpOl83krbj1FLhaK1+UD6E52v7wG++08guf7RIFztEzHp1+RLC1j+sLHqX76+/9dh755Z3/xz23P3g/Oqjr7uX/uB9fPmHlaV0+Z7cqHC6x4T3I/eX9I1EL7BHcfOwhYT0y9tIYwg7NaGrY0KM5GMi2B5IvNC1j7lAPstXhGE5pkQFUpaTdoszIQl9kvYlRpqOoG8+rkgn8YGYBHecjOHFt7t6mYlym2zCFYvCiCP+Jhmf1waHi7lcIvyyBZmfj299M/3tQ5R0kRSlIYBXlQbHd/h5oBViwn4woTP4tPz1VAUTFSFb0hHpigGKA94FKQPbOGkapzShmIWfgGeOYZ+kOD+pBss2pwkjIQZieqGXEiAJZDKj+yhg77yfANr449vrny5K3QB2vPpg4/zlmHrAim12xKLCcpti7v9CuhGYuZ9YZv1bRFLkUH68RKKATIJySTAQhTDlktUh1tyXRuRbieZ69y52r361/un/rH3xfjKqK491Ljk590Ascv3vb3cfXolEIC9A9F/xzt9aXf4c6R6kftQI5OrDO6uPrqXtNaRr7EbkM398OFoglp2NcF5uTCwtfVE63DD0Gw4SIlJKPQVGKX/JQ42EmmNicxN3fRNJHIhb2X37UhVmdUaCwFIIZpFannPMcHWTDFSE2AyXMesbsyzarBYisT3kWyC4tlWX82vH8kt5KO0HraqU0+gENxfg9C62Uep2/G62VTUcR1Qf+Z2Rhq6csuwTFPITvfz6pGA6SUWyXe5YNgrRLtvLrf98+diLXEVCMGSxV0j2SiVXIBFXaelNyvVINItFgeWgIkB3ZltVTdFtICKzjqi2U+EmIotRrWZWQRdWxyV7umbagZ+kOycqZk3Y0L0sZdWyEGcU9aSGMA+qEDB7oBkHRVkALSupKFwb7eEPCuNajZOBo620NE/gXwhmCnY6HOlG7uU02KhinVAD31KtRRJ1lPHbP/ArfeCN4sDegcm5Un64OM/5bLHhZMBRORORU/Yuf6OmL1WdRoKaSK1Q6zTbzHiUFXKIQHWnappjnLXNUUQOmMCfTVYTElLIw+67v1tbWoxKvotC8nGiW0Jh0NAK0XAEnQhR+rQywF0K7mnX1/9Op1o1jFo8rywTVj3nke0FZi45Vc1EZlWfJbmNcXM7djAOyhnXdBFPhAigUiM8o+Bx9il++lRee0ps9Cnknv7xDYSqOAucy3wEksIpG+HkCrGviua5cqaJ9A7NjtIBq9Eh7hcJUKxEjxWotkLMGCjMSQxVwo4RGs9rQ7ncfOrx0kPkmyBRkJjpuPWBPcHpRHU8SLzTIPzyZhBurzlZ/ZRuqnIkK4SAyqN5zc8lFkgI5HMBm43JT1UtsMwmwzZ+jnQSPqZFrV84iAWOAG+inBFWMcRgZjJMMwfz5uIbio4gzxh2VFr3kFYLnRaSLSeQXnVIEscYOU6j0cy+1HlibckEmxppohzA5ZIJRbRMNyyqcNkhax8T4b+tQKrsSqFXwgFAyE2MEuNP9qGRdGHgj49bjkmV7NPGpoASDhxkA83TnHnBvvXOacE3KA8VX4Ab8QWbILOVTKbgJNhNVIdx70ShTTZOdj6PFubE8vNUvtAT3FRl5vOiUGU+ufnaK6nP0ly4TSVB3MWLKbNdCV2mOlLCUMOXoaBUQthwae5TVOyLbGB/D0oVB/72eyjQsENcqZIb5jcJfar9TJ1jEzjXHr3X/f3NtRv3vMdXybO5+C2qBZQQYA/1Ic8uVX9sQ9LLaQTlMb+Ohrthwbg1UR8eDc2g2uCKGuAv89sR7SyU/l2CfXMB92+VIKksGcRkfftSSXmASWwEaizgqFcgfiuHtYVYfQqTRqInB+B0wD2YMlH/NYu0baNt2M+IEkjiDJv8AwfujCPcDXyStY0J6UjhtVBEhRInMjOWlv4IfbsJyHk/FynwxdEJ3+HIJjAn11QwqKCuV9g8oA2iAjmDcppiYRL23NYrmEY6IDlYSQJibUoCcp90b9bvooYe0wN5gSMaTd/0oakkZn5i2vrpSOxFXKLgWiOGnuuZwaGwQU+wkztrGkiqRfI6rBdkFBUNOq8Qoa1gj5Su9r/HxXnYsFUXCZjNK4I6XIXkNIMvLKF8WlOqAI+KQQyWolzkjbmsKGyWUWUgOXwm4pcE42hqRd5WVAODF9gtLMCZdbZBBxgakIL0A3ymg1vRNtmtAPdtb0qxYRorvkV0zHBf/dI/Ub8tV0KRKNvxJBTFc1y0smJJj4BuUrEjMvDwbLsffi392EQNIQqhUKK5fv8ql3FSvSICejJ06JcUpsRRFTDVaxC9wayRYG9IP4U0pKwOZnhlZQJoqZSbGChNypr03Ka7FbP2ApCMFNYQ7Gm1c8IhEcXJvg8VuJvoCkdqcusFGzSxtPf0mmS9XMTn4Uknt1q/kRg5mk72W3OQFEtZgLu9wDWd/eKD7tICqlcFMSXLeQUBJXjHz2KJkLpYPNZFSGdfXQWHwXrLlwaTSgPX+viB0khLoNoyMTzXjEZQMBLDiQRw31h8mjDFlZnsXxj5LziEJK235DX6tBAqHOk4+6Y9HGjEU7fpQCtR4yCcqQaNw9gwbABYbK5Z1Y4br7FSRQGhSWXaiBY3LJSuIA9N9Xa4AmPUqJgfDTSnAnEkjtwv5itix/KH5QRfnRk4sY3wZ2dKhoTDR7NO/3AxzlputSKuM5VbiUciS9kzmOynufKJupd8IqmZGnOe6piNGqnTMBnahJ2b9c8utA4U1cJSC9sroFzo5ESJw0J6TSjlFA0myYcUNN+c4BknfI09mTRxjnIt46ma4qiKwWOhdldoazPrMp+SI0+UCUVJlXcYsYFygaXEwUvftJD3LOKNivGQx124fLotI/qyOZEn2zIxjW8w8ByxNikP8ig1yqUEFediUmvUR09caI2GCFOl1miIvjRpnyKZRuNoluFbihMpp3VKt8mNUgs1E2cchnFjIkQwAng8yglVmyr4K8z8iexUhOY4JByTyDwsrwKmzpf149VAHM+fIa8up9hRHBdL9JJlL1nhBcbNOOM0F+NL5qM1EuWU04gsO8KHAWtmCr/GFZwsXyyaMpDOn6vCukdFY7ls4v5np2ZM23p7xqw6E1wbPFbYs6dchqw1+Ock6AEpwzZbetrTm9utNoXfy+CSYJ1MaKXwFlO3hH+DGDHDSjP4KUJOtfgbe9rfGL6l9cylcmQQPBQRSdUdwD9bMKCnoCBZyAgBXoDDQdyfQXWuLuZMNVXEsJ6myi+pgt03VrpLTygetvhAe804rENHJQt/bCbLUGkUYKdnJ8QixIcDLWvAQXiiMYDrnHrbEIJmwKST0JnFxlqIVLhWWwjRrTkimGFGb7gDVktkgeSslFgcIEpRn0YRPbmlBRy3Rs6Dsq2D47888urhwxSBhbi105q2MrHvlnDS2midHENqyULAoXXStK0Wx55Romy2KnprVoYHZUgajzEw8nw+ecIEU0HEKuiq0NaO2Tt7l9KAnJgTxcSwRIWDgiI03OqVFQ7ffOld/tP67Yur333sfREtQxB5GZluUYW7dLuVYxBKKLeZ1ouqNlnMpLj5cn416km+zbYc2XQ1FuijwVxa+dMYigvS1VSYLeKoDYEbhjZy7MVvU5GlKyxWG76Qp+7+93n/wp50yhLXB/KyFh+S7w14f4G+lC5E5EoBx+HVcEk9tItlEfpocYhybmQiR0dGgiWiyD7aIajFj9cm9XQbudyPvoot2lTtocRNxLj0NLWwCOmuO1cboMiCdXOlQgZipRLo5qgrKqzHrcapBUCci5xtG8L99pebH6VCGj9vNoS0mdQfdNECxqcQKWqScJZikHDdS9KdaBqI3dbIiQgjoWTh6q6LOzlMm6LWPQvLpS6qAFS9nvSiSMOh0wRlwflBxJiYwqWdEwFd5EUEsAL1QT+sqi2+BpHhfCRCzCAGgDmh1SL5S2mTJkg4jn0FLrAgaRbny4Zv00mTZDhidZEvrMxN1Qk8XWpN2yHaUewaaffmgnfnOu6Q0ijcvUUJ1kdvowYXcRs891ZuUdXEjSfixQfazu77X2/cOKfhNi5Kx9dvf4Km6N1+Ql7Fj1Iju0aEKX/6QXmOy9L/RKHRSDedL+5TtcRbEKRx39CnjMaYWrUdxK7hUVr03gk59TbjFKwyYgaGhBFhhlLc7K9a0y1UOlIWW4TniO4KulkIIc6GwYee3kZMgfHuqJaMPyG16n4ScSfXST6+i+v1qJTm0jQJ3jzqEb/l+ugY9EzCvpgRM5Kl9sPK9cBYC7bB8saaahjNijSeJlNq7DpNXAhsZ0FneTk9l8Cg2qzYu4xaITR6ZwbF++hFTh9f6X7wkawQ5Ak06Nm1u38izfvgugwQohLw7Gekgu+8vXblnKgQ3Lh+mUPxaXeVF/GyDfEWl+CdA8AMVfQh+3pxiS5ffngPhTYg2TTzMMSHPCvm8mT4g0NQvhDwMRxBphyYQKLIwFQbeMWIiAeo0Ri83oEMXyiOJikoAzoGNiLuXapMHX/5Bd9fGyzym0xMh69S0ARSQAz6z4nquO5Mbd2zlUikvG7McYEqwDBqlRAeFreQ37jp6Atdnj8loFkXM8nS9B6Bx45N5E9TAIn4kfDwlOnQrL4opJzZuZOOSs5Rzsy4bhusToROM6VOYzQwEfQfDRUCokWd45Nwc59pCLGBJKhZp1oUXRHoxhhhRDWnxnBYYWB+eCSXPplEOI0uJK9kS03MXHWAjmNLsXuFCXulj2MLHJS72O70kFhCaciXE0lz+vocxs/jpURBgBe1b6srt9bvfyK68/uKesEUJ+4UQATuE/oiBcfS6KsQRcdCF/054Udmf/sb5LHcsDAzRVpujgUCm0r9VEhOvi6ukp71TUIvIr99A20cGtjSlMnEctYvuuDHO0UAOVhUpB6S8frUOSvyDRq9b8JskQxkqbcgNU0gluqu50lnJOq/Wb0+hRfKPCVCNaT0+A5acFNZ9lxKaJF+xOYjyJ8lfaSD108YLHvgbwp9CSWIt574hf7BxbLIm0eU7kHqhLv3kJ+hQesrsJ44SqmQn2Mo57k2nyxHqdc3wRtjQSIvFXNS9uw/lHaPuMd94tjYrJ9rykeyhrneIqyXGOuBjMA0EXv2RVufHGYjXQf/S0LMf69XzaAwhwibxZLyMc+AXxpF1hrXK6cZZzC0cJpiy75lBifgfGCYcXZ3CU4ASsV6WFFJy9PXB2KYYn0qbx4LUeWkWQYU11HdB8qrCc+mlzODxYQzgxeBeot/CVwa4cNQpRtb1angR+/Ky+w7S0681AV398VrM7PBG+YqpnijDaM/Lvd6zR9KtlLK26Eim03vI09VBi7IoR/upbI2JarNe9Cd0BDseF1RqiyB0PCRrwzlI1BcmiDxC2KKujTC2YlW04oaJN85D0zwiH+uBgwqMzpirw7iBhOZPbtHhmr1wdLIyNCwPlKq7xopGcN7S4O6PjhY2rurODI4snd4l14b2VUfHBwZ3F2qlYZrxb2lIb1eHNZ3D9Osu+u6USrt2m3sLeqlPaWh6khxaKQ0vLs+NTSyawi/d+8plvZgVrz5dO9w0dBHRoojtd3FvfU91K9EeeUM1apVKEgrcqKMhUfX1u/dSQp7eDxwd8RFWPFitZh30/+NarhQG30f4eLqo3dLuLjmfXoByotMLuJF2tlJXPlFtscHa0AjRmcYNm4/9B5eluUafCYiJoqVRNwUL11bu7Hsnf3b+v2vutfepbceImZ+QzIamH/9swWA3f3tNZSXird3eefOeve+BeRrN99cu/qJWAjgZOb/F09Yd80=")))
names = ("pdf_tools.py", "render_service.py", "render_worker.py", "solver.py", "task_bot.py", "ai.py")
originals = {}
for name in names:
    path = target / name
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"缺少普通程序文件 {name}，未修改程序。")
    originals[name] = path.read_bytes()


def unsupported():
    raise SystemExit("程序与支持的默认升级版不符，尚未修改文件；请使用完整更新包。")


def source(name):
    return originals[name].decode("utf-8-sig").replace("\r\n", "\n")


def replace_once(text, old, new):
    if text.count(old) == 1:
        return text.replace(old, new, 1)
    if old not in text and text.count(new) == 1:
        return text
    unsupported()


interfaces = {"pdf_tools.py": {"render_pdf", "render_pdf_pages", "safe_page_scale"},
              "render_service.py": {"render_artifacts_async", "render_preview_async", "render_input_pdf_async"},
              "render_worker.py": {"main"}}
for name, required in interfaces.items():
    exported = {n.name for n in ast.parse(source(name)).body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if not required <= exported:
        unsupported()

solver = source("solver.py")
classes = [n for n in ast.parse(solver).body if isinstance(n, ast.ClassDef) and n.name == "Solver"]
methods = [n for n in classes[0].body if isinstance(n, ast.AsyncFunctionDef) and n.name == "attachment_inputs"] if len(classes) == 1 else []
if len(methods) != 1:
    unsupported()
method = methods[0]
lines = solver.splitlines(keepends=True)
present = "".join(lines[method.lineno - 1:method.end_lineno])
fingerprint = hashlib.sha256(ast.dump(ast.parse(textwrap.dedent(present))).encode()).hexdigest()
if fingerprint not in payload["method_hashes"]:
    unsupported()
solver = "".join(lines[:method.lineno - 1]) + payload["method"] + "".join(lines[method.end_lineno:])
solver = replace_once(solver,
    "from render_service import render_artifacts_async, render_input_pdf_async",
    "from render_service import render_artifacts_async, render_input_pdf_batches_async")
if "from contextlib import aclosing\n" not in solver:
    solver = replace_once(solver, "import asyncio\n", "import asyncio\nfrom contextlib import aclosing\n")
if "from pdf_tools import PDFError\n" not in solver:
    solver = replace_once(solver, "from render import RenderError\n", "from pdf_tools import PDFError\nfrom render import RenderError\n")

bot = source("task_bot.py")
if payload["help_line"] not in bot:
    anchor = 'HELP = ("我只处理数学物理题及题目追问。直接发送题目/图片/PDF 即可获得题号。\\n"\n'
    bot = replace_once(bot, anchor, anchor + "        " + payload["help_line"] + "\n")
ai = source("ai.py")
if payload["vision_line"] not in ai:
    anchor = "- 保留所有条件、小问、选项、图中标注。\n"
    ai = replace_once(ai, anchor, anchor + payload["vision_line"] + "\n")
candidates = {name: payload[name].encode("utf-8") for name in interfaces}
candidates.update({"solver.py": solver.encode("utf-8"), "task_bot.py": bot.encode("utf-8"), "ai.py": ai.encode("utf-8")})
changes = []
for name in names:
    installed = target / name
    data = candidates[name]
    compile(data, str(installed), "exec")
    previous = originals[name]
    metadata = installed.stat() if previous is not None else (target / "task_bot.py").stat()
    backup = installed.with_name(name + ".before-pdf-input")
    if backup.is_symlink() or (backup.exists() and not backup.is_file()):
        raise SystemExit(f"备份位置异常：{backup.name}，未修改程序。")
    if data != previous:
        changes.append((installed, data, previous, metadata, backup))

if phase == "check":
    for installed, data, previous, metadata, backup in changes:
        if previous is None:
            continue
        try:
            with backup.open("xb") as stream:
                stream.write(previous)
            shutil.copystat(installed, backup)
        except FileExistsError:
            pass
    print("十二个程序文件语法检查通过；原文件备份已保留，不覆盖旧备份，新增模块可首次安装。")
    raise SystemExit(0)

if any(previous is not None and not backup.is_file() for installed, data, previous, metadata, backup in changes):
    raise SystemExit("缺少更新前备份，尚未修改程序。")


def replace_file(path, data, metadata):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target, prefix=".delivery-update-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
        if hasattr(os, "chown"):
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


written = []
try:
    for installed, data, previous, metadata, backup in changes:
        replace_file(installed, data, metadata)
        written.append((installed, previous, metadata))
except BaseException:
    for installed, previous, metadata in reversed(written):
        if previous is None:
            installed.unlink(missing_ok=True)
        else:
            replace_file(installed, previous, metadata)
    print("更新中断，已恢复本次改动前的程序文件。", file=sys.stderr)
    raise
print(f"已更新 {len(changes)} 个程序文件；配置和题目记录保留。")
UPDATE_PDF_PY
}
update_pdf check
systemctl stop qq-math-bot
restart_service() {
  local code=$?
  trap - EXIT
  if ! systemctl start qq-math-bot || ! systemctl is-active --quiet qq-math-bot; then
    echo '服务启动失败，请查看 sudo journalctl -u qq-math-bot -n 100 --no-pager'
    exit 1
  fi
  exit "$code"
}
trap restart_service EXIT
update_pdf install
echo '更新完成：PDF 默认全文读取；同条消息写“只读第3-5页”或“第2、5、8-10页”可选页。'
