import { IElement } from './element';
import { IStep } from './step';

export interface IThread {
  id: string;
  createdAt: number | string;
  // boba: время последней записи истории агента треда
  updatedAt: number | string;
  name?: string;
  userId?: string;
  userIdentifier?: string;
  metadata?: Record<string, any>;
  steps: IStep[];
  elements?: IElement[];
}
