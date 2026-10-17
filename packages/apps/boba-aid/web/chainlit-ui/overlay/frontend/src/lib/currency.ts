// boba: «$» перед цифрой в тексте — деньги, а не начало формулы. remark-math
// читает «цены $5 и $10» как формулу «5 и », поэтому такой «$» экранируется до
// разбора; код (ограды ``` и ~~~, `спаны`) и уже экранированное не трогаются.

const FENCE = /^\s*(`{3,}|~{3,})/;
const DIGIT = /\d/;

function escapeLine(line: string): string {
  let result = '';
  let index = 0;
  while (index < line.length) {
    const char = line.charAt(index);

    if (char === '`') {
      let run = 1;
      while (line.charAt(index + run) === '`') run += 1;
      const close = line.indexOf('`'.repeat(run), index + run);
      if (close < 0) return result + line.slice(index);
      result += line.slice(index, close + run);
      index = close + run;
      continue;
    }

    if (char === '\\') {
      result += line.slice(index, index + 2);
      index += 2;
      continue;
    }

    const single =
      char === '$' &&
      line.charAt(index - 1) !== '$' &&
      line.charAt(index + 1) !== '$';
    if (single && DIGIT.test(line.charAt(index + 1))) {
      result += '\\$';
      index += 1;
      continue;
    }

    result += char;
    index += 1;
  }
  return result;
}

export function escapeCurrency(markdown: string): string {
  const out: string[] = [];
  let fence = '';
  for (const line of markdown.split('\n')) {
    const opened = line.match(FENCE);
    if (fence) {
      out.push(line);
      if (opened && opened[1].charAt(0) === fence.charAt(0)) {
        if (opened[1].length >= fence.length) fence = '';
      }
      continue;
    }
    if (opened) {
      fence = opened[1];
      out.push(line);
      continue;
    }
    out.push(escapeLine(line));
  }
  return out.join('\n');
}
