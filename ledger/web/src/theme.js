/* Naive UI 的主题覆盖。
 *
 * 组件库自带的一套颜色和这套界面的既有规矩对不上：它把主色也用在装饰上，而这里
 * 红黄绿只表达状态（拦住了 / 要留意 / 通过）。一旦装饰用了红色，真正的红色就不再
 * 刺眼，人就会开始忽略警告。所以颜色统一从 design.css 那套变量映过来。
 */

const n = {
  0: '#ffffff', 1: '#f7f8fa', 2: '#eef0f4', 3: '#e2e5eb', 4: '#c9cdd6',
  5: '#9aa1ae', 6: '#6b7280', 7: '#434a56', 8: '#262b34', 9: '#14171c',
}

const accent = '#1f5eff'
const accentHover = '#4a7dff'
const accentPressed = '#1a52e0'

export const theme = {
  common: {
    primaryColor: accent,
    primaryColorHover: accentHover,
    primaryColorPressed: accentPressed,
    primaryColorSuppl: accent,
    successColor: '#0f7b4f',
    successColorHover: '#12925e',
    successColorPressed: '#0c6641',
    warningColor: '#b45309',
    warningColorHover: '#c9610b',
    warningColorPressed: '#93440a',
    errorColor: '#b42318',
    errorColorHover: '#c92a1e',
    errorColorPressed: '#951d14',
    infoColor: n[6],

    textColorBase: n[9],
    textColor1: n[9],
    textColor2: n[8],
    textColor3: n[6],
    borderColor: n[3],
    dividerColor: n[3],
    hoverColor: n[1],
    bodyColor: n[1],
    cardColor: n[0],
    modalColor: n[0],
    popoverColor: n[0],
    tableHeaderColor: n[0],

    borderRadius: '10px',
    borderRadiusSmall: '6px',
    fontSize: '14px',
    fontSizeSmall: '13px',
    fontSizeMedium: '14px',
    fontSizeLarge: '15px',
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", "Segoe UI", Roboto, sans-serif',
    // 金额、行号、单号一律等宽。中文字体里的数字宽度不一致，一列金额会歪得没法比较。
    fontFamilyMono: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
  },
  Card: {
    borderRadius: '14px',
  },
  DataTable: {
    thColor: n[0],
    thTextColor: n[6],
    thFontWeight: '560',
    tdColorHover: n[1],
    borderColor: n[2],
    fontSizeSmall: '13px',
  },
  Layout: {
    siderColor: n[0],
    headerColor: n[0],
  },
}
