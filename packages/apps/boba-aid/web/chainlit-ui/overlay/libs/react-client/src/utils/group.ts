import { IThread } from 'src/types';

export const groupByDate = (data: IThread[]) => {
  const groupedData: { [key: string]: IThread[] } = {};

  const today = new Date();
  today.setHours(0, 0, 0, 0);

  // boba: список стоит по последней активности треда, а не по созданию
  [...data]
    .sort(
      (a, b) =>
        new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime()
    )
    .forEach((item) => {
      const threadDate = new Date(item.updatedAt);
      threadDate.setHours(0, 0, 0, 0);

      const daysDiff = Math.floor(
        (today.getTime() - threadDate.getTime()) / 86400000
      );
      const locale = navigator.language;

      let category: string;
      if (daysDiff === 0) {
        category = 'Today';
      } else if (daysDiff === 1) {
        category = 'Yesterday';
      } else if (daysDiff <= 7) {
        category = 'Previous 7 days';
      } else if (daysDiff <= 30) {
        category = 'Previous 30 days';
      } else {
        category = threadDate.toLocaleString(locale, {
          month: 'long',
          year: 'numeric'
        });
      }

      groupedData[category] ??= [];
      groupedData[category].push(item);
    });

  return groupedData;
};
