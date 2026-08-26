import type { ReactElement } from "react";

import type { SpecIssue } from "../../model/spec";

type Props = {
  issues: SpecIssue[];
};

/** Замечания валидации: код, место, текст. */
export function IssueList({ issues }: Props): ReactElement | null {
  if (issues.length === 0) {
    return null;
  }

  return (
    <ul className="issues">
      {issues.map((issue, index) => (
        <li className="issues__item" key={`${issue.code}:${issue.where}:${index}`}>
          <span className="issues__code mono">{issue.code}</span>
          {issue.where !== "" && <span className="issues__where mono">{issue.where}</span>}
          <span>{issue.message}</span>
        </li>
      ))}
    </ul>
  );
}
