import {Tag} from '@dagster-io/ui-components';
import React from 'react';

import {DagsterTag} from '../runs/RunTag';

export const RunlessEventTag: React.FC<{
  tags: Array<{__typename: 'EventTag'; key: string; value: string}>;
}> = ({tags}) => {
  const user = tags.find((t) => t.key === DagsterTag.ReportingUser);

  // Note: This does not use UserDisplay because in cloud, the UserDisplay component is hardcoded to a
  // 14pt font size. I think it'd be nice to make that component more flexible and revisit.
  return user ? <Tag>Reported by {user.value}</Tag> : <Tag>Reported</Tag>;
};
